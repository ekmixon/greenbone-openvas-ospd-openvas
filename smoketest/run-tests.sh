#!/usr/bin/env bash
# Is a convenience script to start redis, ospd-openvas and execute smoketests

shutdown() {
  kill $(cat /run/ospd/ospd.pid) || true
  kill $(cat /tmp/mosquitto.pid) || true
  kill $(grep -o "Pidfile.*" /etc/ssh/sshd_config | awk '{printf $2}') || true
  redis-cli -s /run/redis/redis.sock SHUTDOWN
}

trap shutdown EXIT

set -e
mosquitto -c /etc/mosquitto.conf &
redis-server /etc/redis/redis.conf
/usr/sbin/sshd
ospd-openvas --disable-notus-hashsum-verification True \
  -u /run/ospd/ospd-openvas.sock \
  -l /var/log/gvm/ospd.log
wait_turn=0
while [ ! -S /run/ospd/ospd-openvas.sock ]; do
  if [ $wait_turn -eq 10 ]; then
    printf "too many attempts to find ospd-openvas.sock\n"
    exit 1
  fi
  printf "waiting for ospd-openvas.socket ($wait_turn)\n"
  sleep 1
  wait_turn=$(($wait_turn + 1))
done
/usr/local/bin/ospd-openvas-smoketests
