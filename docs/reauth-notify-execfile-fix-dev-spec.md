# Dev-spec: reauth-healer notify.sh escalation csendes hibája

Kanban: 98a2b3ea. Urgent, jóváhagyva (BÉLA kérése 2026-08-29).

## Probléma

2026-08-29 kb. 15:35-17:34 között BÉLA (fő-agent) saját OAuth tokenje halott
lett. A reauth-healer (`src/web/reauth-healer.ts`) HELYESEN detektálta ezt
ismételten (~3 percenkénti poll), de MINDEN escalation-próbálkozás
`notify.sh escalation failed` WARN-nal végződött (`execFile` hiba a
`sendNotify()`-ban) -- Istvan SOHA nem kapott automata Telegram-értesítést,
csak akkor vette észre, amikor próbált írni és nem jött válasz.

## Reprodukciós kísérletek (mindkettő SIKERTELEN -- a hiba NEM
reprodukálható izoláltan)

1. `env -i` + a futó `bela-dashboard.service` processz TÉNYLEGES
   környezeti változóival (`/proc/<pid>/environ`-ból kiolvasva: PATH, HOME,
   LANG, XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS, stb) közvetlenül bash-sel
   futtatva a `scripts/notify.sh`-t: **sikeres, exit 0**.
2. 1:1 Node.js `execFile('/bin/bash', [NOTIFY_SCRIPT, msg], { timeout:
   10_000 }, ...)` hívás, ugyanazzal a környezettel, `stdin < /dev/null`
   (nincs TTY, mint a systemd-processznél): **sikeres, exit 0, "Ertesites
   elkuldve."**.

A `systemctl --user cat bela-dashboard.service` ellenőrzés kizárta a
sandboxing-hipotézist is (nincs `PrivateTmp`/`ProtectHome`/hasonló
direktíva). A dashboard.log egy IZOLÁLT, NEM egyidejű hiba-esetet is mutat
(18:12:53, egyetlen "dead OAuth token" ERROR + egyetlen "notify.sh
escalation failed" WARN, semmilyen párhuzamos hívás nélkül) -- ez cáfolja a
kártya (a) versenyhelyzet-hipotézisét mint EGYEDÜLI okot (bár burst esetén
hozzájárulhat).

**A pontos gyökérok a bash/execFile útvonalon belül nyitva marad.** Nem
találtam ki hozzá magyarázatot, és ezt nem hallgatom el.

## Döntés: nem tovább vadászni, hanem kiiktatni a bizonytalan útvonalat

Ahelyett hogy tovább kerestem volna egy nem-reprodukálható, megmagyarázatlan
shell-alfolyamat-hibát, a `sendNotify()` átállt a `sendTelegramMessage()`
(`src/web/telegram.ts`) közvetlen `fetch()`-alapú küldésére -- UGYANARRA a
mechanizmusra amit a `schedule-runner.ts` sajátjai (pending-retry,
task-timeout, catch-up summary) MÁR bizonyítottan megbízhatóan használnak,
nincs rájuk vonatkozó hasonló panasz. Eggyel kevesebb folyamathatár (nincs
bash, nincs curl, nincs a notify.sh saját `tmux display-message` alapú
sender-detekciója, ami a dashboard processznek amúgy sem tudna érdemben
válaszolni, hiszen az maga nem tmux-kliens) -- eggyel kevesebb hely ahol egy
megmagyarázatlan hiba történhet. Emellett a `sendTelegramMessage()` hiba
esetén a TÉNYLEGES HTTP-státuszt és választörzset dobja (`Telegram API
<status>: <body>`), nem csak egy csupasz exit code-ot -- ez direkt válasz a
kártya 2. pontjára (jobb hiba-log) is, extra munka nélkül.

## Változtatások

- `src/web/telegram.ts`: új export `resolveTelegramBotToken()`, kiszervezve
  a `schedule-runner.ts`-ből (volt `resolveSchedulerAlertToken`, privát) --
  egyetlen forrás mindkét hívónak, ugyanaz a fallback-sorrend
  (`PROJECT_ROOT/.env` majd `~/.claude/channels/telegram/.env`, mint a
  `notify.sh`).
- `src/web/schedule-runner.ts`: a privát `resolveSchedulerAlertToken`
  eltávolítva, a 3 hívási hely átállítva az importált
  `resolveTelegramBotToken()`-re. Viselkedés VÁLTOZATLAN.
- `src/web/reauth-healer.ts`: `sendNotify()` most `resolveTelegramBotToken()`
  + `sendTelegramMessage(token, ALLOWED_CHAT_ID, msg)`-t hív, ugyanazzal a
  config-hiányra figyelmeztető guard-mintával mint a schedule-runner
  alertjei. `NOTIFY_SCRIPT`/`join`/`PROJECT_ROOT` import törölve (már nem
  kellenek -- `execFile` marad, a fájl MÁS célra (tmux /login send-keys,
  kill-session) még használja).

## Hatókör -- amit SZÁNDÉKOSAN nem érintettem

A `scripts/notify.sh`-t más helyek is hívják (`channel-coordinator.ts`
konkrétan, `agent-worker.ts`/`auth-gate.ts`/`test-run-marker.ts` csak
megjegyzésben említik). Ellenőriztem a dashboard.log-ot a
`channel-coordinator: notify.sh alert failed` mintára -- ma NULLA találat,
de ez NEM bizonyíték hogy az az útvonal biztosan jó (lehet hogy egyszerűen
nem volt ma escalation-triggere). A kártya kifejezetten a reauth-healer
hibáját jelezte, ezért CSAK azt javítottam -- a channel-coordinator saját
notify.sh-hívása külön, jövőbeli vizsgálat tárgya lehet ha hasonló tünet
jelentkezik ott is.

## Tesztelés

- `src/__tests__/reauth-healer.test.ts`: új fix-revert guard blokk
  (forrás-scan, mert `sendNotify()` privát, fire-and-forget, nincs
  visszatérési értéke amit assertálni lehetne) -- ellenőrzi hogy
  `sendTelegramMessage(token, ALLOWED_CHAT_ID, msg)` jelen van és
  `NOTIFY_SCRIPT`/`notify.sh` NINCS jelen a forrásban.
- Meglévő `decideReauthAction` tesztek változatlanok (a pure döntési logikát
  nem érintette ez a fix, csak a delivery-mechanizmust).

## Deploy

Szokásos folyamat: `npx tsc --noEmit`, git worktree-ben teljes teszt,
commit, `npm run build`, `systemctl --user restart bela-dashboard.service`
(előre ellenőrizve `ss -ltnp` vs `systemctl show -p MainPID` egyezés).
