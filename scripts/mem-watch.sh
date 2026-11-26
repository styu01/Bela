#!/bin/bash
# Logs memory every 5s; if MemAvailable < 1 GB, SIGTERMs the codex app-server to protect the VM.
LOG=/home/kisss/marveen/store/mem-watch.log
for i in $(seq 1 5760); do
  av=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  sw=$(awk '/SwapFree/{print int($2/1024)}' /proc/meminfo)
  top=$(ps -eo rss,pid,args --sort=-rss | awk 'NR>1&&NR<=4{printf "%dM/%s/%s | ",$1/1024,$2,substr($3,length($3)-30)}')
  echo "$(date +%T) avail=${av}M swapfree=${sw}M $top" >> $LOG
  if [ "$av" -lt 1000 ]; then
    echo "$(date +%T) GUARD: low memory, SIGTERM codex app-server" >> $LOG
    pkill -TERM -f "codex app-server" 
  fi
  [ $(stat -c %s $LOG) -gt 5000000 ] && tail -n 2000 $LOG > $LOG.tmp && mv $LOG.tmp $LOG
  sleep 5
done
