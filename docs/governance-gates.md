# Governance-kapuk (PreToolUse gate-ek): működés, önteszt, migrációs szabály

## Mik a kapuk és hol vannak bekötve

| Kapu | Kinél | Matcher | Bekötés |
|------|-------|---------|---------|
| `scripts/email-send-gate.mjs` | minden sub-ágens | `Bash\|.*send_email.*\|.*manage_email.*` | `agent-scaffold.ts` injectEmailSendGate |
| `scripts/self-pace-gate.mjs` | minden sub-ágens | `ScheduleWakeup\|Cron*\|RemoteTrigger\|Bash\|Write\|Edit\|NotebookEdit` | injectSelfPaceGate |
| `scripts/kanban-write-gate.mjs` | csak a rejtett heartbeat-worker | `Bash` | injectKanbanWriteGate |
| `scripts/hooks/egress-gate.mjs` | mindenki, a fő ágens is (`~/.claude/settings.json`, ebből másolódik a `.channels-config/settings.json`) | `WebFetch` | injectEgressGate / ensureEgressGate |
| `scripts/hooks/outgoing-copy-gate.py` | fő ágens (`.claude/settings.json`, kézzel írt, verziókövetett) | Bash, send_email, Gmail, Telegram reply | kézi |

A sub-ágensek settings-ét a dashboard minden spawnkor újragenerálja, a boot pedig az
`ensureEgressGate` / `ensureGovernanceGateCommands` hívással helyben frissíti (a régi bejegyzést a
szkript NEVE alapján cseréli, nem duplikál).

## Fail-closed

Claude Code egy hívást csak 2-es exit kódra (vagy stdout-on adott deny-ra) tilt; minden más státusz
nem blokkoló hiba. Ezért a kapu-parancs vége egy közös farok (`failClosedGateRun`, upstream 0e681c83):
0 és 2 marad, minden más (betöltési hiba, hiányzó fájl, jel, a saját határidő) 2 lesz. Ha egy kapu
fájlja elromlik, az összes illeszkedő hívás TILTVA lesz. A stderr-üzenet megnevezi a kaput és a
kiutat: Bash nélkül is javítható, mert a fő ágens Read/Edit/Write eszköze nincs kapuzva.

Szándékos kivétel: a fő ágens Telegram-válasz ága (outgoing-copy-gate a Telegram matcheren) fail-open
marad, mert a Telegram a tulajdonos egyetlen felügyeleti csatornája. Ez a hiányzó fájlra is igaz: a parancs
elején `[ -f "$CLAUDE_PROJECT_DIR/scripts/hooks/outgoing-copy-gate.py" ] || exit 0`, mert a python3 hiányzó
fájlnál 2-vel lép ki, és a 2 TILTÁS lenne (a Telegram-válaszok blokkolódnának). Ilyenkor a válaszok
ellenőrzés nélkül mennek ki; az önteszt ezt külön jelzi („a kapu nem fut, a hívások ellenőrzés NÉLKÜL
mennek ki”), mert a bekötött kapu-fájl meglétét is vizsgálja.

## Önteszt (`src/web/gate-self-test.ts`)

Bootkor (+90 mp) és 15 percenként minden bekötött kapu-parancs pontosan úgy fut, ahogy a settings-ben
áll, egy ártalmatlan hívással, és a kapu-fájl meglétét is ellenőrzi. Ha egy kapu nem 0-val lép ki, tilt,
vagy a fájlja hiányzik, kapu-szkriptenként EGY eszkaláció indul (`[kapu-onteszt] KAPU HIBA ...`; előbb a fő
ágens, ha megmarad, a tulajdonos), és ha a kapu minden bejegyzése újra rendben, egy `HELYREALLT` üzenet.
Időtúllépés jellegű hibánál (határidő, kill, jel) 60 mp múlva egyszer újrafuttatja, és csak a második
hibánál riaszt, a load-dal együtt. Fail-closed bejegyzésnél a szöveg „TILTVA”, a burkolatlan (fail-open)
Telegram-ágnál „a kapu nem fut, a hívások ellenőrzés NÉLKÜL mennek ki”. A boot-prune
„megtartott” esete (fail-closed kapu, amelynek fájlja hiányzik az élő gyökérben) ugyanígy jelez
(`HIANYZO KAPU-FAJL`). Worktree / ideiglenes / WEB_ONLY példány nem futtatja.

## MIGRÁCIÓS SZABÁLY: kapu átnevezése vagy kivezetése (BÉLA 6223 c)

Mióta a kapuk fail-closed módon futnak, egy kapu-fájl átnevezése, áthelyezése vagy törlése
**migrációt igényel**, különben a flotta tilt: a már kiírt settings-bejegyzések a régi útra mutatnak,
a hiányzó fájl 1-es (node) vagy 2-es (python) exit kódot ad, a farok ezt 2-re fordítja, és a boot-prune
az élő gyökérben hiányzó fail-closed kaput szándékosan MEGTARTJA (különben a kapu csendben eltűnne).
Egy elveszett self-pace-gate után a sub-ágensek minden Bash/Write/Edit hívása tiltva van.

Ezért egy ilyen változtatás (saját fejlesztés VAGY upstream-átvétel) csak így mehet élesbe:

1. Ugyanabban a változtatásban legyen migráció, amely minden érintett settings-fájlban (fő ágens
   `~/.claude/settings.json` és `.claude/settings.json`, minden `agents/*/.claude/settings.json`)
   lecseréli vagy eltávolítja a régi bejegyzést, mielőtt a régi fájl eltűnik. Kényelmes út: a régi
   fájl egy élesítési ciklusig maradjon meg (akár csak továbbhívó csonkként), és a boot-migráció írja
   át a bejegyzéseket.
2. Frissüljön a `GATE_SCRIPT_NAMES` (`gate-self-test.ts`), a `KNOWN_HOOK_SCRIPTS`
   (`hook-registration-guard.ts`) és a kapura hivatkozó tesztek.
3. Élesítés után a kapu-önteszt első köre (90 mp) ne jelezzen hibát; ha jelez, azonnal vissza.
4. Upstream-triage-nél egy kaput érintő átnevezés / kivezetés külön tételként, ezzel a szabállyal
   megjelölve kerüljön a listára (lásd `~/.claude/skills/upstream-fork-triage-adopt`).
