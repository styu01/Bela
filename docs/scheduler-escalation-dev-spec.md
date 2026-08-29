# Dev-spec: schedule-runner két-lépcsős riasztás-eszkaláció

Kanban: 26009690. Jóváhagyva (Istvan 2026-08-29 08:44, a kártya maga a jóváhagyás).

## Probléma

`sendPendingRetryAlert` és `sendTaskTimeoutAlert` (`src/web/schedule-runner.ts`) ma
KÖZVETLENÜL Telegramon értesítik Istvant (`ALLOWED_CHAT_ID`), amint egy küszöbidő
lejár -- akkor is, ha a probléma magától megoldódik mielőtt BÉLA egyáltalán
észlelhetné. 2026-08-29 reggel három feladat (usage-monitor, reggeli-napindito,
kanban-audit) mindegyike 60+ percig várt egy foglalt session miatt, mindhárom
KÜLÖN Telegram-riasztást váltott ki, majd mind a három magától lefutott amint a
session felszabadult -- felesleges ijesztés egy önmagát megoldó helyzetre.

## Cél

Két lépcső, ugyanaz az elv mindkét mechanizmusnál:

1. **1. lépcső (meglévő küszöb):** inter-agent üzenet BÉLÁ-nak
   (`createAgentMessage('system', MAIN_AGENT_ID, ...)`), NEM Telegram. BÉLA
   látja a session-jében, kivizsgálhatja/reagálhat.
2. **2. lépcső (ÚJ, nagyobb küszöb):** csak akkor megy ki Telegramon
   KÖZVETLENÜL Istvannak, ha a probléma az 1. lépcső óta további
   `OWNER_ESCALATION_EXTRA_MS`-ig (75 perc) is fennáll. Ez a végső,
   soha-nem-néma tartalék-csatorna (feedback_no_silent_scheduler_failures
   memória -- ezt NEM töröljük, csak később lép életbe).

A két lépcső EGYMÁSTÓL FÜGGETLENÜL, mindkettő a `firstAttempt`/`injectedAt`
időponthoz képest számolt küszöbbel dől el (nem egymásra láncolva) -- ha az
1. lépcső Küldése bármiért meghiúsulna (pl. DB-hiba), a 2. lépcső akkor is a
saját küszöbénél lefut, a "soha ne legyen néma hiba" elv szerint. Ez
szándékos, konzisztens az `usage-monitor` hiszterézis-mintájával (állapot-
alapú, nem egymásra épülő láncolat) és nem hoz létre új csendes hibamódot.

## 1) pending_task_retries (sendPendingRetryAlert)

- `src/pending-retries.ts`: új export `OWNER_ESCALATION_EXTRA_MS = 75 * 60_000`
  és `OWNER_ALERT_THRESHOLD_MS = ALERT_THRESHOLD_MS + OWNER_ESCALATION_EXTRA_MS`
  (135 perc összesen). `toPendingRetryView()` két új mezőt kap:
  `ownerAlertSentAt: row.owner_alert_sent_at`,
  `ownerAlertDue: shouldSendAlert(now, row.first_attempt, row.owner_alert_sent_at, ownerThresholdMs)`
  -- a MEGLÉVŐ `shouldSendAlert` pure függvény újrahasznosítva, nem új logika.
- `src/db.ts`: `CREATE TABLE` marad változatlan (friss telepítésnél már benne
  van), utána `try { ALTER TABLE pending_task_retries ADD COLUMN owner_alert_sent_at INTEGER } catch {}`
  a meglévő migrációs idióma szerint (lásd pl. `alert_sent_at` melletti
  mintát ugyanebben a fájlban). Új függvények, az `alert_sent_at`-osok
  pontos tükörképei: `markPendingTaskRetryOwnerAlert`,
  `clearPendingTaskRetryOwnerAlert`.
- `src/web/schedule-runner.ts`:
  - ÚJ `sendPendingRetryBelaNotice(view, now)`: 1. lépcső, `markPendingTaskRetryAlert`-tal
    stamppel (meglévő, változatlan), `createAgentMessage('system', MAIN_AGENT_ID, text)`
    hívással bela session-be. Hiba esetén (try/catch) NEM tartja meg a
    stampet -- `clearPendingTaskRetryAlert` hívódik, hogy a következő tick
    újrapróbálja (ugyanaz a claim-before-send / clear-on-failure minta mint
    a Telegram-útnál, csak a hibaosztályozás nélkül, mert egy lokális
    DB-insert hibája mindig retry-érdemes, nincs "permanens" változata mint
    a Telegram 4xx-nél).
  - `sendPendingRetryAlert` (MEGLÉVŐ, VÁLTOZATLAN belső logika/szöveg) mostantól
    a 2. lépcső: `markPendingTaskRetryOwnerAlert`-tal stampel (owner-oszlop),
    `clearPendingTaskRetryOwnerAlert`-tal töröl hiba esetén.
  - Hívási hely (kb. 1209. sor): `if (stillPresent && view.alertDue) sendPendingRetryBelaNotice(view, now)`
    ÉS külön `if (stillPresent && view.ownerAlertDue) sendPendingRetryAlert(view, now)`.

## 2) task_inflight (sendTaskTimeoutAlert) -- CSAK memóriában, nincs DB-tábla

A kártya "allapot-tablak"-nak nevezi, de a `task_inflight` valójában a
`taskInflightMap` in-memory Map (`TaskInflightEntry[]`), NEM DB-tábla --
pontosítás, nem hiba a kártyában, csak fontos a helyes implementációhoz: az
"owner notified" jelző itt egy ÚJ mezővel az interface-en, nem DB-oszloppal,
és dashboard-restart után NEM marad meg (ugyanaz a korlát mint a meglévő
`alerted` mezőnél -- konzisztens, nem új gyengeség).

- `TaskInflightEntry`: új mező `ownerAlerted: boolean` (induláskor `false`,
  ugyanott ahol `alerted` inicializálódik).
- `decideTaskTimeout()` (pure, unit-tesztelt): bővítve `ownerAlerted` bemenettel
  és `opts.ownerExtraMs` opcióval. Visszatérési típus bővül:
  `'clear' | 'alert' | 'escalate' | 'hold'`.
  - `alert`: változatlan feltétel (busy && elapsed >= timeoutMs && !alerted).
  - ÚJ `escalate`: `busy && alerted && !ownerAlerted && elapsed >= timeoutMs + opts.ownerExtraMs`.
  - `clear`/`hold` egyéb esetek változatlanok.
  - Megjegyzés a kódban: `ownerExtraMs` hozzáadása elméletileg átlépheti
    `maxTrackMs`-t egy már eleve nagy `stuckAfterMinutes`-re konfigurált
    feladatnál (a bejegyzés `maxTrackMs`-nél kilép mielőtt escalate
    kiértékelődne) -- ismert, elfogadott korlát, nem ezen kártya hatóköre.
- Hívási hely (kb. 1153. sor): `alert` esetén ÚJ `sendTaskInflightBelaNotice(entry, elapsed)`
  hívódik (inter-agent BÉLÁ-nak) a jelenlegi Telegram-hívás HELYETT, majd
  `entry.alerted = true`. `escalate` esetén a MEGLÉVŐ `sendTaskTimeoutAlert(entry, elapsed)`
  (változatlan direkt Telegram + kanban-waiting side effect) hívódik, majd
  `entry.ownerAlerted = true`.
- A `markScheduledTaskKanbanWaiting` side effect ÁTKERÜL a bela-notice-ba
  (1. lépcső) -- a tábla-státusz frissítés hasznos jelzés akkor is ha végül
  nem escalálódik Istvanig, és nincs értelme megvárni a 2. lépcsőt vele.

## Konzisztencia-ellenőrzés (a kártya minőségi mandátuma)

- **usage-monitor flotta-pause hiszterézis**: state-fájl alapú, aszimmetrikus
  küszöb (pause >=90%, resume csak <80%-nál, a köztes sáv szándékosan
  "ne csinálj semmit" hogy ne pattogjon). A mi tervünk ebből az ELVET veszi
  át (állapot-alapú one-shot stamp mindkét lépcsőn, nem ismétlődő/pattogó
  riasztás) -- ez MÁR megvan a meglévő `alert_sent_at`/`alerted` mintában,
  a 2. lépcső ugyanezt a mintát ismétli meg egy második stamppel, nem
  talál ki új mechanizmust.
- **sub-agent liveness check (10 perc rákérdezés)**: BÉLA-szintű, emberi
  protokoll (nem kód) -- onnan a releváns elv a HATÁROLT követési ablak
  (nem végtelen csendes várakozás), ami a mi tervünkben az
  `OWNER_ESCALATION_EXTRA_MS` fix küszöbként jelenik meg. A konkrét 10 perc
  nem másolható át 1:1 (más nagyságrendű probléma -- egy sub-agent
  élet-jelre percek alatt válaszol, egy foglalt session felszabadulása
  eleve órás skálán mozog, lásd a meglévő 60 perces `ALERT_THRESHOLD_MS`-t),
  de az ELV (ne várj örökké csendben, de adj ésszerű időt mielőtt
  eszkalálsz) ugyanaz.

## Tesztelés

- `src/__tests__/pending-retries.test.ts`: új `describe` blokk
  `OWNER_ALERT_THRESHOLD_MS` / owner-mezők köré, ugyanolyan mintázatú esetek
  mint a meglévő `shouldSendAlert`/`toPendingRetryView` teszteknél (határeset
  a küszöbön, már-küldött owner-alert nem duplikálódik, stb).
- `src/__tests__/schedule-task-timeout.test.ts`: bővítve `escalate` esetekkel
  a `decideTaskTimeout` `describe` blokkban -- alert utáni escalate, escalate
  utáni hold (ownerAlerted=true), maxTrackMs elsőbbsége escalate felett.

## Deploy

Szokásos folyamat: `npx tsc --noEmit`, git worktree-ben teljes teszt (élő
telepítés nem futtat tesztet közvetlenül), commit, `npm run build`,
`systemctl --user restart bela-dashboard.service` (NEM bela-channels --
lásd korábbi saját tévedés 2026-08-26-i memóriában), port/PID/journal/HTTP
ellenőrzés utána.
