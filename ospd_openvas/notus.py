# -*- coding: utf-8 -*-
# Copyright (C) 2014-2021 Greenbone Networks GmbH
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from pathlib import Path
from time import sleep
from typing import Any, Dict, Iterator, Optional, Callable
from threading import Timer
import json
import logging

from redis import Redis

from ospd.parser import CliParser
from ospd_openvas.messages.result import ResultMessage

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self, db: Redis, prefix: str = "internal/notus/advisories"):
        self.db = db
        self.__prefix = prefix

    def store_advisory(self, oid: str, value: Dict[str, str]):
        return self.db.lpush(f"{self.__prefix}/{oid}", json.dumps(value))

    def exists(self, oid: str) -> bool:
        return self.db.exists(f"{self.__prefix}/{oid}") == 1

    def get_advisory(self, oid: str) -> Optional[Dict[str, str]]:
        result = self.db.lindex(f"{self.__prefix}/{oid}", 0)
        if result:
            return json.loads(result)
        return None

    def get_keys(self) -> Iterator[str]:
        for key in self.db.scan_iter(f"{self.__prefix}*"):
            yield str(key).rsplit('/', maxsplit=1)[-1]


class Notus:
    """Stores and access notus advisory data in redis"""

    cache: Cache
    loaded: bool = False
    loading: bool = False
    path: Path
    _verifier: Callable[[Path], bool]

    def __init__(
        self,
        path: Path,
        cache: Cache,
        verifier: Callable[[Path], bool],
    ):
        self.path = path
        self.cache = cache
        self._verifier = verifier

    def reload_cache(self):
        if self.loading:
            # block until loading is done
            while not self.loading:
                sleep(1)
            return
        self.loading = True
        self.loaded = False
        for f in self.path.glob('*.notus'):
            if self._verifier(f):
                data = json.loads(f.read_bytes())
                advisories = data.pop("advisories", [])
                for advisory in advisories:
                    res = self.__to_ospd(f, advisory, data)
                    self.cache.store_advisory(advisory["oid"], res)
            else:
                logger.log(
                    logging.WARN, "ignoring %s due to invalid signature", f
                )
        self.loading = False
        self.loaded = True

    def __to_ospd(
        self, path: Path, advisory: Dict[str, Any], meta_data: Dict[str, Any]
    ):
        result = {}
        result["vt_params"] = []
        result["creation_date"] = str(advisory.get("creation_date", 0))
        result["last_modification"] = str(advisory.get("last_modification", 0))
        result["modification_time"] = str(advisory.get("last_modification", 0))
        result["summary"] = advisory.get("summary")
        result["impact"] = advisory.get("impact")
        result["affected"] = advisory.get("affected")
        result["insight"] = advisory.get("insight")
        result['solution'] = "Please install the updated package(s)."
        result['solution_type'] = "VendorFix"
        result['vuldetect'] = (
            'Checks if a vulnerable package version is present on the target'
            ' host.'
        )
        result['qod_type'] = meta_data.get('qod_type', 'package')
        severity = advisory.get('severity', {})
        cvss = severity.get("cvss_v3", None)
        if not cvss:
            cvss = severity.get("cvss_v2", None)
        result["severity_vector"] = cvss
        result["filename"] = path.name
        cves = advisory.get("cves", None)
        xrefs = advisory.get("xrefs", None)
        advisory_xref = advisory.get("advisory_xref", "")
        refs = {}
        refs['url'] = [advisory_xref]
        advisory_id = advisory.get("advisory_id", None)
        if cves:
            refs['cve'] = cves
        if xrefs:
            refs['url'] = refs['url'] + xrefs
        if advisory_id:
            refs['advisory_id'] = [advisory_id]

        result["refs"] = refs
        result["family"] = meta_data.get("family", path.stem)
        result["name"] = advisory.get("title", "")
        result["category"] = "3"
        return result

    def get_filenames_and_oids(self):
        if not self.loaded:
            self.reload_cache()
        for key in self.cache.get_keys():
            adv = self.cache.get_advisory(key)
            if adv:
                yield (adv.get("filename", ""), key)

    def exists(self, oid: str) -> bool:
        return self.cache.exists(oid)

    def get_nvt_metadata(self, oid: str) -> Optional[Dict[str, str]]:
        return self.cache.get_advisory(oid)


class NotusResultHandler:
    """Class to handle results generated by the Notus-Scanner"""

    def __init__(self, report_func: Callable[[list, str], bool]) -> None:
        self._results = {}
        self._report_func = report_func

    def _report_results(self, scan_id: str) -> None:
        """Reports all results collected for a scan"""
        results = self._results.pop(scan_id)
        if not self._report_func(results, scan_id):
            logger.warning(
                "Unable to report %d notus results for scan id %s.",
                len(results),
                scan_id,
            )

    def result_handler(self, res_msg: ResultMessage) -> None:
        """Handles results generated by the Notus-Scanner.

        When receiving a result for a scan a time gets started to publish all
        results given within 0.25 seconds."""
        result = res_msg.serialize()
        scan_id = result.pop("scan_id")
        timer = None

        if not scan_id in self._results:
            self._results[scan_id] = []
            timer = Timer(0.25, self._report_results, [scan_id])
        self._results[scan_id].append(result)

        if timer:
            timer.start()


DEFAULT_NOTUS_FEED_DIR = "/var/lib/notus/advisories"


class NotusParser(CliParser):
    def __init__(self):
        super().__init__('OSPD - openvas')
        self.parser.add_argument(
            '--notus-feed-dir',
            default=DEFAULT_NOTUS_FEED_DIR,
            help='Directory where notus feed is placed. Default: %(default)s',
        )
        self.parser.add_argument(
            '--disable-notus-hashsum-verification',
            default=False,
            type=bool,
            help=(
                'Disables hashsum verification for notus advisories.'
                ' %(default)s'
            ),
        )
