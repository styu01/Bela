# Dev-spec: reauth-healer false dead-token restart — recent-task sanity check

Státusz: TERV, kód még nincs írva. Kanban kártya (reauth-healer hamis
dead-token restart) alapján, BÉLA delegálta (msg 1029/1039). István döntése:
**A) opció** — józanság-ellenőrzés a legutóbbi sikeres scheduled-task alapján,
NEM a tail-fallback szűkítése (az B) opció volt, nem választotta).

Előzmény/gyökérok-elemzés: ld. az inter-agent jelentés (msg 1030) — a
2026-08-24-i 4 restart (09:02, 12:05, 15:05, 18:06) mind pontosan
`ESCALATION_COOLDOWN_MS` (3 óra) távolságra esett egymástól, minden esetben
`reason: "Not logged in"`, sehol nem volt valódi 401/quarantine-log — ez azt
jelenti hogy a `consecutiveDead` számláló **soha nem nullázódott** egész nap,
miközben 16:00/17:00/18:00-kor bizonyíthatóan sikeres LLM-hívást igénylő
scheduled task futott le ugyanazon a session-ön. Gyanús mechanizmus:
`reauth-detect.ts` `liveStatusRegion()` `null`-t ad, ha a pane-ben nincs
látható input-doboz (pl. aktív generálás/eszközhasználat közben), és ekkor a
kód szándékosan visszaesik a nyers 15-soros `tailOf()` scrollback-scanre,
ami a `REAUTH_MARKERS` bármelyikére tüzel — kontextus-tudatosság nélkül.

## 1. Cél

Mielőtt a reauth-healer kényszer-restartolja a MAIN agentet (`restartMain`),
nézze meg: futott-e le sikeresen scheduled task ugyanezen a session-ön az
elmúlt ~20 percben. Ha igen, ez erős ellenbizonyíték egy "halott OAuth
token" olvasatra szemben — egy valódi halott tokennel fizikailag nem
futhatott volna le egy LLM-hívást igénylő task.

## 2. Adatforrás: `schedule-runner.ts` in-flight task tracking

A modul már követi, mely session-ökön fut éppen egy nemrég injektált
scheduled task (`taskInflightMap`, `schedule-runner.ts:130`), és a sweep
ciklusban (`schedule-runner.ts:1103-1117`) törli a bejegyzést amikor a pane
`idle`-re vált (`decideTaskTimeout()`, 148-161. sor: `paneState === 'idle'`
→ `'clear'`) — ez pontosan a "a task sikeresen lefutott, a session
visszatért idle állapotba" pillanat. Jelenleg ez a pillanat **nincs sehol
eltárolva**, csak törlődik a Map-ből.

**Változtatás**: új, session-kulcsú modul-szintű állapot:

```ts
// schedule-runner.ts, a taskInflightMap közelében
const lastTaskCompletedAtMs = new Map<string, number>()

/**
 * Milliseconds-epoch timestamp of the last time a scheduled task on this
 * session cleanly finished (pane returned to idle after injection) -- NOT
 * when it merely fired, and NOT when it was evicted via maxTrackMs (that is
 * the "never went idle, probably stuck" path, the opposite of liveness
 * evidence). null if unknown/never observed since process start.
 *
 * Consumed by reauth-healer.ts as a sanity check before a main-agent
 * force-restart on a "dead OAuth token" reading -- a session that
 * demonstrably just completed an LLM-backed task cannot simultaneously have
 * a dead token. See docs/reauth-sanity-check-dev-spec.md.
 */
export function getLastTaskCompletedAt(session: string): number | null {
  return lastTaskCompletedAtMs.get(session) ?? null
}
```

A sweep ciklusban (1103-1117. sor környékén), a `paneState === 'idle'` ágon
(a `decideTaskTimeout()` hívás előtt/mellett, mivel a `state` változó már ott
van kiszámolva):

```ts
if (state === 'idle') {
  lastTaskCompletedAtMs.set(entry.session, now)
}
```

Ez KÜLÖN feltétel a `decision === 'clear'`-től (ami a maxTrackMs-eviction
ágat IS lefedi — azt szándékosan NEM számítjuk sikeres befejezésnek).

**Miért ez az adatforrás, nem a log-parszolás**: a `dashboard.log` grep-elése
törékeny (formátum-függő, lassú nagy fájlon, és a healer saját folyamatában
nem is éri el más folyamat log-fájlját megbízhatóan real-time-ban) — az
in-memory Map ugyanabban a Node-folyamatban él mint a reauth-healer maga
(mindkettő a `startReauthHealer()`-t és `startScheduleRunner()`-t is ugyanaz
a szerver-folyamat indítja), egyszerű, szinkron, azonnal konzisztens.

**Körkörös import-kockázat**: ellenőrizve — `schedule-runner.ts` NEM
importál semmit `reauth-healer.ts`-ből vagy `reauth-detect.ts`-ből
(`grep -n reauth schedule-runner.ts` nulla találat), tehát egy sima statikus
`import { getLastTaskCompletedAt } from './schedule-runner.js'` a
`reauth-healer.ts`-ben biztonságos, nincs szükség a `channel-monitor.js`-nél
használt dinamikus import-workaroundra.

## 3. `decideReauthAction()` bővítése — tiszta, tesztelhető döntési logika

Új mező az `ReauthHealerInput`-ban:

```ts
export interface ReauthHealerInput {
  // ...meglévő mezők változatlanul...
  /**
   * Milliseconds since a scheduled task last cleanly completed on this
   * session, or null if unknown. Only consulted for the main agent (isMain)
   * -- sub-agents keep the existing behavior unchanged, this is scoped to
   * the reported main-session force-restart problem.
   */
  msSinceLastCompletedTask: number | null
}
```

Új mező a thresholds objektumban:

```ts
export interface ReauthHealerThresholds {
  threshold: number
  cooldownMs: number
  /** Window within which a completed task counts as liveness proof (main agent only). */
  recentTaskLivenessWindowMs: number
}
```

A függvény elején, MINDJÁRT a meglévő clean-probe ág után, ÚJ ág:

```ts
export function decideReauthAction(input: ReauthHealerInput, t: ReauthHealerThresholds): ReauthHealerDecision {
  const { isDeadToken, sessionAlive, isMain, canInteractiveLogin, isFirstRunGate, msSinceLastCompletedTask, prev, nowMs } = input

  if (!isDeadToken || !sessionAlive) {
    return { sendKeys: false, restartAgent: false, restartMain: false, escalate: false, next: NO_REAUTH_STATE }
  }

  // Sanity check (main agent only, István döntése 2026-08-25): a session,
  // ami bizonyíthatóan az imént fejezett be egy LLM-hívást igénylő
  // scheduled taskot, nem lehet EGYIDEJŰLEG halott OAuth tokennel -- a
  // marker-alapú "dead" olvasat ilyenkor félreolvasás (ld. dev-spec 
  // gyökérok-elemzés: tail-fallback + a session épp aktívan dolgozik, nincs
  // látható input-doboz). Ugyanúgy kezeljük mint egy tiszta probe-ot: a
  // spell véget ér, nem indul restart/escalate ebből a körből.
  if (isMain && msSinceLastCompletedTask != null && msSinceLastCompletedTask <= t.recentTaskLivenessWindowMs) {
    return { sendKeys: false, restartAgent: false, restartMain: false, escalate: false, next: NO_REAUTH_STATE }
  }

  // ...a többi, meglévő logika változatlan...
}
```

### Miért "resetelje a consecutiveDead-et", nem csak "blokkolja a restartMain-t egy körre"

BÉLA explicit felvetette mindkét irányt (msg 1039). A reset mellett döntök,
indoklással:

- Ha CSAK a `restartMain`-t blokkolnánk, de `escalate` (Telegram-riasztás a
  tulajdonosnak) és a `consecutiveDead` növekedés változatlanul futna,
  Istvánhoz akkor is megérkezne egy félrevezető "halott OAuth token" üzenet
  annak ellenére, hogy a kezünkben van ellenbizonyíték ugyanabban a
  pillanatban — ez nem jobb, csak kevésbé súlyos hiba.
- A teljes reset (`NO_REAUTH_STATE`, pontosan mint egy tiszta probe) azt
  jelenti: a következő probe-nál a rendszer friss szemmel, 0-ról újra
  felépíti a `consecutiveDead`-et, ha a pane TÉNYLEG rossz állapotot mutat.
  Mivel a döntés minden 3 percben újra lefut élő adatok alapján (nem egy
  elavult "blokkolt state"), ez önmagát korrigálja: ha 20 percen túl NEM jön
  újabb sikeres task-befejezés, a normál escalate/restart-logika a
  megszokott módon (3 egymást követő dead probe → escalate) újra aktívvá
  válik.
- A reset NEM rejt el egy valódi, tartósan fennálló hibát: a
  `recentTaskLivenessWindowMs` (20 perc) jóval rövidebb mint a
  `ESCALATION_COOLDOWN_MS` (3 óra), tehát ha a token TÉNYLEG halott marad,
  legfeljebb ~20 percenként (amíg új scheduled task fut és sikeresen
  befejeződik) kaphat "bye" jelet a spell — utána, ha tényleg nincs több
  sikeres task-befejezés, a normál escalate-ág 9 percen belül újra épül és
  tüzel.

## 4. `checkSession()` bővítése (`reauth-healer.ts`)

```ts
import { getLastTaskCompletedAt } from './schedule-runner.js'

const RECENT_TASK_LIVENESS_WINDOW_MS = 20 * 60 * 1000 // 20 min

function checkSession(label: string, session: string, isMain: boolean, quiet: boolean): void {
  const pane = capturePane(session)
  const sessionAlive = pane != null
  const reauth = detectReauthNeeded(pane)
  const prev = watchState.get(session) ?? NO_REAUTH_STATE
  const isFirstRunGate = /onboarding picker|sign-in screen/i.test(reauth.reason ?? '')

  const lastCompleted = isMain ? getLastTaskCompletedAt(session) : null
  const msSinceLastCompletedTask = lastCompleted != null ? Date.now() - lastCompleted : null

  const decision = decideReauthAction(
    {
      isDeadToken: reauth.needsReauth,
      sessionAlive,
      isMain,
      canInteractiveLogin: hostCanInteractiveLogin(),
      isFirstRunGate,
      msSinceLastCompletedTask,
      prev,
      nowMs: Date.now(),
    },
    { threshold: DEAD_PROBE_THRESHOLD, cooldownMs: ESCALATION_COOLDOWN_MS, recentTaskLivenessWindowMs: RECENT_TASK_LIVENESS_WINDOW_MS },
  )

  // ...a többi, meglévő logika változatlan...
}
```

Javasolt egy extra log-sor is a láthatóság kedvéért, amikor a sanity-check
ténylegesen felülbírált egy dead-olvasatot (segít a jövőbeli hasonló
esetek diagnosztizálásában):

```ts
if (isMain && reauth.needsReauth && sessionAlive && msSinceLastCompletedTask != null && msSinceLastCompletedTask <= RECENT_TASK_LIVENESS_WINDOW_MS) {
  logger.info(
    { label, session, reason: reauth.reason, msSinceLastCompletedTask },
    'reauth-healer: dead-token reading overridden -- session completed a scheduled task within the liveness window',
  )
}
```

(Ez a log-hívás a `checkSession()`-ben megy, MIELŐTT a `decideReauthAction`-t
hívjuk, mert utána már nem tudjuk különválasztani "ez volt az ok" a "simán
nem ért el a threshold-ig" esettől.)

## 5. Hatókör -- mi NEM változik

- **Sub-agentek** (`isMain === false`): a `sendKeys`/`restartAgent` útvonal
  változatlan marad. A jelentett probléma kifejezetten a MAIN session
  kényszer-restartjáról szólt (context-vesztés); a sub-agentek eltérő,
  kevésbé súlyos heal-útvonalon mennek (best-effort /login vagy egyszerű
  restart, nem teljes kontextus-nullázás), és a schedule-runner in-flight
  tracking-je is a MAIN session-re fut jellemzően (a `MAIN_CHANNELS_SESSION`
  kapja a legtöbb scheduled/heartbeat taskot) -- a hatókör szándékosan szűk,
  a "ne bővítsd csendben" elv szerint.
- **`reauth-detect.ts` tail-fallback logika**: VÁLTOZATLAN marad (ez volt a
  B) opció, Istvan nem ezt választotta). A meglévő
  `'DOES fire when the marker is in the live tail'` teszt is változatlan.
- **Quiet-hours logika, escalation-cooldown érték, első-futás-gate kezelés**:
  változatlan.

## 6. Tesztek

Új unit tesztek a meglévő `reauth-healer.test.ts` mintája szerint:
1. `isMain=true`, `isDeadToken=true`, `sessionAlive=true`,
   `msSinceLastCompletedTask=5*60_000` (5 perc) → `escalate=false`,
   `restartMain=false`, `next=NO_REAUTH_STATE`.
2. Ugyanaz, de `msSinceLastCompletedTask=25*60_000` (25 perc, kívül az
   ablakon) → a normál dead-token logika fut (escalate/restart a threshold/
   cooldown szerint, mint eddig).
3. Ugyanaz, de `isMain=false` → a sanity-check NEM alkalmazandó, a normál
   sub-agent logika fut változatlanul, még akkor is ha volna
   `msSinceLastCompletedTask` adat.
4. `msSinceLastCompletedTask=null` (nincs ismert adat) → a normál logika fut,
   ahogy eddig.

Új teszt a `schedule-runner.ts`-hez: `getLastTaskCompletedAt()` visszaadja a
helyes timestampet idle-clear után, és `null`-t/korábbi értéket ad
maxTrackMs-eviction (stuck task) esetén -- azaz a stuck-task ág NEM
szennyezi be a liveness-jelet.

## 7. Kanban-bontás

1. `schedule-runner.ts`: `lastTaskCompletedAtMs` + `getLastTaskCompletedAt()`
   export, idle-clear ágban feltöltve
2. `reauth-healer.ts`: `ReauthHealerInput`/`ReauthHealerThresholds` bővítés,
   `decideReauthAction()` sanity-check ág, `checkSession()` bekötés + log
3. Unit tesztek (mindkét fájlhoz)
