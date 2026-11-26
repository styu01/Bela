# Pinterest Buffer feltöltés modell nélkül

A feltöltő kizárólag a Bufferben már létező, előzetesen jóváhagyott draftokat időzíti. Új pint nem hoz létre és jóváhagyást nem végez. A napi 07:00-s user timer indítja; normál feltöltésnél csak egy JSON-sort ír a journalba. Telegramot nem hív.

## István beállításai

A beállítás a `store/pinterest-schedule.json`. A feltöltő csak olvassa; szerkesztésre a CLI való. Minden óra és dátum **America/New_York, azaz ET szerint** értendő, a futtató timer pedig Europe/Budapest szerint működik. A napot az ET-dátum határozza meg, nem a budapesti vagy UTC-dátum.

Példák a repó könyvtárából, későbbi kézi végrehajtásra:

```text
python3 scripts/pinterest-schedule-config.py show
python3 scripts/pinterest-schedule-config.py set-day sat off
python3 scripts/pinterest-schedule-config.py set-day sun off
python3 scripts/pinterest-schedule-config.py skip 2026-11-27
python3 scripts/pinterest-schedule-config.py pause-until 2026-11-09
python3 scripts/pinterest-schedule-config.py pause-until off
python3 scripts/pinterest-schedule-config.py set-day mon 8,12,19
python3 scripts/pinterest-schedule-config.py unskip 2026-11-27
python3 scripts/pinterest-schedule-config.py reset
```

Az első két `set-day` hétvégén nem enged új posztot. A `skip` november 27-ét hagyja ki. A `pause-until 2026-11-09` november 9. végéig szünetelteti a feltöltést, november 10-től ismét mehet. A pause mindig a dátum végéig, bezárólag érvényes. A `show` a következő 14 ET-nap darabszámát és lehetséges óráit írja ki, fájlt nem hoz létre. A `reset` minden napra visszaállítja a korábbi hét órát: 8, 10, 12, 14, 16, 19, 21.

**A már Bufferben időzített pineket a konfiguráció nem törli, nem mozgatja és nem vonja vissza.** A beállítás az új ütemezésekre hat. Ha a meglévő sor posztolását is szüneteltetni kell, azt a Bufferben külön kell kezelni.

A teljes formátum négy kötelező mezője: `slots_et_by_weekday` hét napkulccsal (`mon`…`sun`), `skip_dates_et` dátumlista, `pause_until_et` dátum vagy null, `max_pins_per_day` egész 0–24. Üres naplista kihagyás; nulla napi maximum vagy minden nap off csendes paused állapot. A napi maximum alapértéke 7, kézi átállítását csak érvényes teljes konfiguráción, futó feltöltésen kívül végezd. Az órák 0–23 közötti, szigorúan növekvő egészek, ismétlés nélkül. A skip és pause megelőzi a heti órarendet. A CLI `--state-dir`, `--now` és `--dry-run` kapcsolói a parancs elé kerülnek.

Hiányzó fájl a régi alapbeállítást jelenti. Sérült JSON, ismétlődő JSON-kulcs, hiányzó/idegen mező, hibás nap/óra/dátum **schedule_invalid**, és nem történik Buffer-módosítás. Hibás fájlnál nincs csendes alapérték. A CLI mutáló parancsai is visszautasítják, csak a kifejezett `reset` írja felül alapértékkel. A CLI és feltöltő közös flockot használ, így nem változik az órarend egy futó feltöltés alatt. A CLI atomikusan ír; ütközéskor busy hiba után később ismételd meg.

## Hogyan választ időpontot?

A Buffer Free csatornalimit továbbra is 10 időzített pin. A feltöltő a meglévő sor vége után, és legalább a mostani idő plusz 15 perc után keres új helyet. Beszámítja a már időzített pineket az ET-napi maximumba. A kiválasztás legfeljebb a mai ET-nappal kezdődő 14 naptári napot vizsgálja, a 15. napot már nem. A tavaszi óraátálláskor nem létező helyi órát kihagyja; az őszi ismételt órát egyszer, első előfordulásában használja.

Ha egyetlen szabad hely sincs ebben az ablakban, `no_slot_within_14_days` hiba jön, végtelen ciklus nélkül. Ritka ütemezésnél azonban nem követeli meg a tízes sor feltöltését: az elérhető néhány helyet időzíti, `scheduled` és `reason=no_slot_within_14_days` eredménnyel. Így például heti egy pin is működik. Ha éppen érvényes a pause, még Buffer-lekérdezést sem végez, és csendesen paused marad. A next-slot szabály ettől függetlenül minden dátumon betartja az inkluzív pause/skip tiltást.

Módosítás előtt a kiválasztott teljes helyi tervet ellenőrzi: egyértelmű `Dxx_Pn` pin-mappa, minden kötelező pin.txt mező, ismert board, HTTP(S) link/kép és helyi időpont. A `utm_content=D…_P…` nélkülieket a korábbi működéshez hasonlóan nem időzíti, és nem számolja az azonosítható jóváhagyott készletbe. Dupla pin-azonosság hibát ad. A pinfájl nem mutathat symlinken a pin-gyökéren kívülre. Nincs shell vagy fájlból végrehajtott utasítás. A Buffer GraphQL fetch-query az eredetiével whitespace-en kívül egyezik, az editPost ugyanazt a korábbi műveletet használja.

## Kimenet és BELA-értesítés

A stdout utolsó és jelenleg egyetlen sora JSON: `status`, `scheduled_count`, `added`, `next_due_utc`, `drafts_left`, `days_of_drafts_left`. A státusz queue_full, scheduled, no_drafts_left, paused vagy error. Az `added` az ebben a futásban visszaigazolt új ütemezések száma, a next_due_utc a legkorábbi még esedékes, ismert sorbeli időpont. Pause/busy esetén a nem lekérdezett számok null értékűek, nem kitalált nullák. Részleges API-hibánál az addig visszaigazolt darabszámot és következő időpontot megőrzi.

A készletnap-becslés a következő 14 ET-nap konfigurált napi kapacitásának átlaga: megmaradt draft × 14 / összkapacitás. A heti off, skip, pause, napi maximum és DST-ből kieső óra is csökkenti a fogyási becslést. Nulla kapacitásnál az eredmény null, nem nulla nap. Ez átlagos naptári napokra becslés, nem a következő poszt pontos ellátási határideje, és a már időzített tíz pin nem része a draftkészletnek. Nem minden megmaradt pinhez olvas újra pin.txt-t, csak a most kiválasztottakhoz; a készlet ismert UTM-azonosságú, Buffer-draft státuszú tételek száma.

A szkript `[PINTEREST]` inter-agent üzenetet küld bela→bela kizárólag hibánál, elfogyott draftnál, vagy két napnál kisebb becsült készletnél. Ezek három külön ok, okonként és budapesti naponként legfeljebb egyszer. Egy futásban csak a legfontosabb okot jelzi: error, majd no_drafts_left, majd low_drafts. Az üzenet ékezet és tiltott gondolatjel nélküli fix szöveg, nem tartalmaz API-választ vagy kulcsot. Jelentése: BELA nézze át a hibakódot, illetve időben kérjen új napi pin-jóváhagyást Istvántól. A `pinterest-buffer-state.json` csak ezek sikeres API-elfogadásának dátumait tárolja; egy normál, elég készletet hagyó feltöltés csendes.

A kulcs csak a tényleges API-híváskor olvasódik a state-dir `.buffer-api-key` fájljából. Hiánya fix `buffer_credentials_unavailable`, importáláskor nincs kulcsolvasás/traceback. HTTP-, GraphQL-, hibás nyugta- és lapozási hibák fix kódok. Ismétlődő cursor és 100 oldal fölötti lapozás leáll. Proxy és HTTP redirect nincs. A helyi dashboard tokent a közös kanban-audit transport kezeli. Kulcs, nyers HTTP-törzs, mappalista és nyers kivétel nem kerül a kimenetbe.

Értesítési hiba után nem ment sikeres napi jelzést, ezért újrapróbálható. Olvashatatlan dedup-state esetén hibával megáll; nem küld ellenőrizetlenül, ismétlési korlát nélkül. Sérült JSON új üres jelzési állapotot jelent. A jövőbeli jelzési bélyeget nem használja tartós némításra. Sikeres POST és sikertelen helyi mentés között ismétlődhet egy jelzés. Buffer-részhiba után a következő futás frissen lekéri a sort/draftokat, nem játssza újra a helyi tervet. Távoli atomikus tranzakció és konzisztencia-garancia nincs; az elfogadott edit nyugtája a post id/státusz/dueAt értékével ellenőrzött.

A `--dry-run` csak a lekérdezéseket és a helyi ellenőrzést végzi, terv szerinti előnézeti számokat ad `dry_run:true` mellett. Nem hív editPost-ot vagy dashboard POST-ot, nem ír configot, állapotot, lockot vagy bytecode-ot. A valódi Buffer-lekérdezés is hálózati művelet; itt csak mockolt API-val teszteltem, éles dry runt sem végeztem.

## Telepítés, kizárólag későbbi operátori műveletek

1. **A régi pinterest-buffer-topup Claude-heartbeatet a timer engedélyezése ELŐTT le kell tiltani.**
2. A patch új fájlként tartalmazza a feltöltőt. Az éles repóban lévő régi példány nem verziókövetett; alkalmazás előtt helyezd biztonsági mentésbe, hogy az útvonal felszabaduljon. A kiinduló fájlt a munkavégzés közben nem módosítottam az éles repóban.
3. Alkalmazd a patch-et. A service két `/path/to/marveen` helyőrzőjét töltsd ki, ellenőrizd a pin-gyökeret, a két privát kulcsfájl hozzáférését és a kívánt órarendet. Ne írasd ki a kulcsokat. A közös kanban-audit és pinterest_schedule.py segédfájl kell.
4. A user service/timer sablont ellenőrzés után telepítse/engedélyezze az operátor. Naponta 07:00 Europe/Budapest, Persistent=true. Oneshot, Restart=on-failure, RestartSec=300, StartLimitIntervalSec=3600, StartLimitBurst=3; az első indítást is számolja. A 600 másodperces service-timeouttal is az egyórás ablakon belül marad három indítás. Az első futás után journalctl-lel ellenőrizd a JSON-t, ne Claude heartbeat jelentésére várj.

A naptárkifejezés ellenőrzése helyes 2026-10-24 07:00 CEST és október 25. 07:00 CET eredményt adott. Teljes unit-verify, éles service-/WSL-próba, telepítés vagy engedélyezés nem történt.

## Teszt és külön kritikus stresszkör

```text
pinterest-buffer-topup.test.py: Ran 44 tests in 0.346s, OK
pinterest-schedule-config.test.py: Ran 10 tests in 0.053s, OK
kanban-audit.test.py: Ran 60 tests in 0.848s, OK
```

A stresszkörben javítottam a ritka, két héten belül a tízes sornál kisebb kapacitás esetét: a rendelkezésre álló helyek is feltöltődnek, nem egy teljesíthetetlen tízes terv blokkol mindent. Részleges távoli hiba esetén a JSON most megőrzi a már visszaigazolt előrehaladást, készletbecslést és next_due-t. Hibás/dupla JSON-kulcs nem írhatja felül észrevétlenül a skip-szándékot. Tesztelt a napi cap, 14. nap kizárása, ET spring gap/fall fold, hibás fájl, duplicate pin, UTM-be rejtett félrevezető szöveg, GraphQL titkos hibaszöveg, jövőbeli értesítési bélyeg, atomikus mentési hiba és sikertelen értesítés újrapróbálása. Két valódi feltöltőfolyamat közös fake távoli soron pontosan tíz külön pint időzített, nem húszat. A CLI ugyanazt a lockot és validálást használja.

Minden Buffer-hívás mockolt, a szintetikus pinfájlok ideiglenes könyvtárban voltak. Az eredeti éles Buffer API-t, PIN_ROOT tartalmát és kulcsokat nem olvastam próbaküldéshez. Nem történt commit, push, dashboard-újraindítás, telepítés vagy aktiválás. Az API-kontraktus extra nyugtaellenőrzése éles válasszal nem ellenőrzött; részleges hálózati bizonytalanság és értesítés/nyugtamentés közti duplázás megmaradó korlát.
