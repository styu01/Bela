# Modell nélküli heti elakadás-audit

A `scripts/heti-elakadas-audit.py` hétfőnként rövid állapotellenőrzést végez. Nem használ modellt, nem javít hozzárendelést, nem archivál és nem pingeli a felelősöket. Ha nincs kivétel, nem küld üzenetet. Ha talál valamit, egyetlen összefoglalót ad BELA-nak a helyi dashboardon keresztül, `[HETI-AUDIT]` előtaggal, ékezet és gondolatjel nélkül. A kártyaadatokat idézett adatként kezeli, és kéri BELA emberi felülvizsgálatát.

## Mit figyel?

* A `pending_task_retries` sorait: több mint 50 próbálkozás vagy szigorúan két óránál régebbi utolsó próbálkozás már jelzés.
* A nem archivált, nem done kártyák hozzárendelését. Üres felelősnél csak a címben VAGY a leírásban szereplő adminisztrációs kulcsszó számít: admin-felület, jóváhagyás, jóváhagy, WP beállítás, engedély, hozzáférés, belépés. A szkript ékezet- és kis/nagybetű-függetlenül keres; a kötőjeles admin-felületet is felismeri. Egy név, például István vagy progi említése már nem elég. Alágenshez rendelt kártyánál ugyanezeket a szavakat csak a címben keresi. Az API-ból megismert új alágensekre is működik a második ág. BELA és István nem alágens.
* A `store/dashboard.log` új soraiban a `Stuck-input` és `Schedule target session busy` mintákat. Egy sort egyszer számol akkor is, ha mindkettő benne van. Szigorúan 30 000 fölötti növekmény vált ki jelzést. A küszöb a mért heti alapszint többszöröse, nem a korábbi 500/nap × 7 becslés.
* A helyi `GET /api/agents` életjelét. A futtatni kívánt neveket a `store/agents-desired.json` fájlból olvassa. Az ezek közé tartozó ágens leállása vagy API-listából hiánya jelzés. A leállított autobot és usage-tracker normális, ha nincsenek a desired listában. Bármelyik alágens `needsReauth:true` értéke ettől függetlenül jelzés, BELA önmagát kihagyja. Hibás, hiányos vagy duplikált API-adat nem számít tiszta állapotnak.

A `DEFAULT_ALWAYS_RUNNING` fallback, `SUB_AGENTS`, `ADMIN_KEYWORDS`, `REPORT_GAP` és `LOG_THRESHOLD` a fájl tetején változtatható. A két hygiene-ág közös admin-kulcsszólistát használ. Üres vagy hibás listaelem `invalid_hygiene_keywords` hibát ad, mert egy üres sztring minden kártyára illeszkedne. A hozzárendelési keresés egyszerű szabály, nem érti a mondatok jelentését: adminisztrációs szó említése téves találat lehet, egy szinonima kimaradhat. Ezért csak felülvizsgálati jelzés, soha nem automatikus javítás. A cím és leírás külön-külön legfeljebb első 100 000 normalizált karakterében keres. A cím vége és a leírás eleje együtt nem alkothat mesterséges találatot. A kártyánkénti 28 napos ismétlési korlát mindkét hozzárendelési ágra érvényes, nem függ a cím apró változásaitól. A retry és az ágenshibák heti újrajelzése nincs ezzel elnémítva.

### Futtatni kívánt ágensek és mért log-alapszint

A tényleges fájlformátum a dashboard `src/web/agent-desired-state.ts` olvasójával egyezően JSON-tömb, például `["progi","iris","vizsla"]`. Nem `{ "agents": [...] }` objektum. A fájlt minden valódi auditban újra beolvassa, nem induláskor cache-eli. A nevek kisbetűs, betűvel kezdődő ágensazonosítók; az ismétléseket egyszer veszi figyelembe. BELA és az emberi István kimarad.

Hiányzó, üres, sérült, nem tömb, olvashatatlan vagy hibás neveket tartalmazó fájlnál a fallback: progi, okoska, kisprogi, iris, deepseek, vizsla. Ha a szűrés után csak a tulajdonosok maradnának, szintén fallback. Ez kifejezetten az audit óvatos tartaléklistája; a dashboard saját olvasója üres készletet ad hibánál. A JSON-összefoglaló `desired_source=file` vagy `fallback` és normál auditban `desired_count` mezője mutatja a használt módot, fájltartalmat és hibaszöveget nem naplóz. Az érvényes desired lista helyettesíti a fallbacket, nem összeadódik vele. Egy újonnan felvett ágens így kódmódosítás nélkül figyelt lesz. Az ágens-regiszter ismert nevei a hygiene-ellenőrzésben ettől függetlenül használhatók.

A `LOG_THRESHOLD=30000` továbbra is a fájl tetején állítható. BELA közölt mérései alapján a log kb. 1 MB/nap tempóban nő, a heti találatszám kb. 8000–14 000; az utolsó 1 MB-ban 2009 találat volt. Ezek átvett mérések, nem itt végzett új éles mérés. A 30 000 a tipikus 10–14 ezres hét kb. 2–3-szorosa; a teljes 8–14 ezres sávhoz viszonyítva kb. 2,1–3,75-szörös. Célja a megszokott forgalom fölötti kiugrás jelzése, nem a régi 500/nap × 7 képlet. Pontosan 30 000 még csendes, 30 001 már jelez. Kimaradt futásoknál továbbra is az előző sikeres scan óta felgyűlt összes sor számít, nem napi átlag.

## Dátum nélküli napló

Az állapotfájl `store/heti-elakadas-audit-state.json`. A `last_audit_at` és `reported` mellett a `log` mezőben tartalmazza a device/inode azonosítót, fájlméretet, bájtoffsetet, az inicializálás óta összesített találatszámot és forgatásszámot. Az első sikeres futás a meglévő napló végére áll; a régi 16 MB-ot nem olvassa végig és nem riaszt rá. Ez csak a napló kezdeti alapvonala: a többi három ellenőrzés már az első futáskor is jelezhet.

Később az új tartalmat 64 KB-os darabokban olvassa, a futás elején mért fájlvégig. A menet közben hozzáfűzött sorokat a következő futás látja. Az ANSI-kódokat bájtszinten kezeli, hibás UTF-8 sem szakítja meg a feldolgozást. Csak LF-fel lezárt sor számít; egy félbeszakadt sort és a darabhatáron félbevágott keresett mintát a következő futás folytatja. Ehhez csak prefixhosszakat és néhány jelzőt ment, nyers naplószöveget nem. A félbemaradt ANSI-parancs állapotát is viszi, de sorhatárnál lezárja, nehogy egy rossz escape a további naplót elrejtse.

Inode-váltásnál vagy kisebb méretnél az új fájl elejéről olvas, `rotated` vagy `truncated` állapottal. A régi offset előtti legfeljebb 64 bájtról tárolt hash segít felismerni azt is, ha a fájlt csonkolták és közben a régi méreténél nagyobbra nőtt: `rewritten`. Ez legfeljebb néhány 64 bájtos ellenőrző visszaolvasást jelent, nem a régi napló újraolvasását. A hash nem tartalmaz visszaállítható naplószöveget.

A heti küszöb ténylegesen az előző sikeres futás óta felhalmozódott új sorokra vonatkozik; az időpont nélküli logból naptári hetet nem próbál kitalálni. Kimaradt futás után az intervallum hosszabb lehet, a küszöb akkor is 30 000. Forgatás előtt még feldolgozatlan, de már elvitt régi fájlvég elveszhet: archivált logokat nem követ. Azonos inode, visszanőtt méret és azonos ellenőrző hash mellett egy teljesen azonos szövegű újraírás nem bizonyítható. A scan alatti észlelt forgatás/csonkolás hibát ad, az offset nem lép előre.

## Használat és hibák

```text
/usr/bin/python3 scripts/heti-elakadas-audit.py --dry-run
/usr/bin/python3 scripts/heti-elakadas-audit.py --state-dir /tmp/audit-store --db /tmp/audit.db --now 2026-11-09T09:00:00+01:00 --dry-run
```

### Csendes induló feltöltés

Telepítéskor egyszer, kézzel futtatható mintaparancsok, itt nem végrehajtva:

```text
/usr/bin/python3 /path/to/marveen/scripts/heti-elakadas-audit.py --seed-reported --dry-run
/usr/bin/python3 /path/to/marveen/scripts/heti-elakadas-audit.py --seed-reported
```

A `--seed-reported` a pillanatnyi hygiene-találatokat felveszi a már jelentettek közé, mindkét ágon. Csak a még nem jelentett vagy már legalább 28 napja jelentett találat kap új időpontot. A friss jelentési időt nem tolja tovább, ezért ugyanazon halmazra közeli ismétlésnél a state byte-ra változatlan, nincs új fájlírás. Más kártyák jelentéseit megőrzi. A 28 napos védelem nem örök felmentés: a határ után az audit ismét jelezhet.

A seed a normál flock és atomikus mentés alatt fut, de nem teljes audit: az érvényes korábbi `last_audit_at` és `log` állapotot változtatás nélkül menti vissza. Nem olvassa és nem inicializálja a naplót, annak hiánya sem akadály. Hiányzó state esetén ezek null értékűek maradnak. A következő normál audit a logot rendesen inicializálja, és a seedelt kártyajelentéseket megőrzi. Az azonos audit-időpont nem nyomja el a seedet.

Az aktuális alágensnevek azonosításához a seed is csak olvasási célú helyi agents GET-et végez. Sem normál, sem best-effort hiba-POST-ot nem küld, hiba esetén kizárólag fix stderr/JSON és exit 1 marad. A retry- és életjelproblémákra seed közben nem jelez, és nem állítja, hogy ezeket auditálta. Dry run mellett sem state, sem lock, sem bytecode nem íródik. Az első normál auditban a régi napló `initialized` állapota önmagában nem riaszt, de egy új, nem seedelt adminisztrációs találat, valódi retry vagy ágenshiba továbbra is jelez.

A `--now` Unix-másodperc vagy időzónás ISO-idő. Kapcsolók nélkül a szkript saját repohelyéből számítja az abszolút `store` útvonalat, nem az aktuális cwd-ből. A `--state-dir` a token, állapot és dashboard.log könyvtára is. A `--db` külön DB-t enged. A `--dashboard-url` alapértéke `http://localhost:3420`; csak localhost/127.0.0.1/::1 HTTP-cím és opcionális helyi port engedett, proxy és átirányítás nincs.

A script a már beépített `scripts/kanban-audit.py` segédfüggvényeit és POST-transportját használja, kizárólag Python standard könyvtárral. A token a `.dashboard-token` fájlból jön. Nincs Telegram, külső API, shell vagy modell. Az adatbázis `mode=ro`, `query_only` és olvasási tranzakció; a kártyák és retry-k közös DB-pillanatképből származnak. A dry run sem helyi fájlt, sem bytecode-cache-t nem ír, nem hoz létre lockot, és POST-ot sem küld, hiba esetén sem. **Az életjel GET-et dry runban is elvégzi**, különben nem tudna hiteles eredményt adni. Ez csak a helyi dashboard olvasása. Nem üres SQLite WAL mellett a dry run írásmentesen leáll; ilyenkor checkpointolt másolaton lehet előnézetet készíteni, a szkript nem checkpointol éles DB-t.

A teljes normál futást flock védi, második párhuzamos példány `busy` eredménnyel tér vissza. A mentés a kanban-audit 0600-as tmp, fsync, atomikus replace és directory-fsync mintáját követi. Normál auditban csak teljes ellenőrzés és szükség esetén sikeres API-nyugta után menti a kurzort, jelentéseket és auditidőt. A seed külön karbantartó ág, csak a jelentési halmazt bővíti. Sikertelen normál üzenet után minden újrapróbálható marad. A távoli elfogadás és a helyi mentés közti leállás ugyanakkor ismételt üzenetet okozhat; közös tranzakció nincs. A POST-nyugta sorba állítást igazol, nem BELA tényleges olvasását.

Hiányzó/sérült JSON új naplóalapvonalat indít; ha olvasható állapotban a log-offset vagy részleges felismerési állapot hibás, látható `state_read_failed` hiba keletkezik. DB-, API-, log- vagy más futási hibánál stdout-ra egy JSON-sor, stderr-re egy fix hibakód és exit 1 jön. Normál auditmódban, seed nélkül megpróbál egy rövid `[HETI-AUDIT] a szkript hibaval allt le: <fix hibakod>` üzenetet is küldeni BELA-nak. Ha a dashboard nem elérhető, ez is meghiúsulhat; a másodlagos kivételt elnyeli. Normál POST-hiba esetén ezért legfeljebb még egy best-effort hibajelző POST történhet. Token, URL, HTTP-választörzs, nyers kivétel és kártyacím nem kerül stdout/stderr-be. Nincs külön naplófájl, a service journalt használ.

## User timer, kizárólag sablon

A service és timer a `scripts/systemd/heti-elakadas-audit.*` fájlokban van. **A régi heti-elakadas-audit heartbeatet a timer engedélyezése ELŐTT le kell tiltani.** A service két `/path/to/marveen` helyőrzőjét kell cserélni, `/usr/bin/python3` abszolút, PATH-tól nem függ. Hétfő 09:00 Europe/Budapest, `Persistent=true`; a VM/user manager működése továbbra is üzemeltetési előfeltétel.

A service `Restart=on-failure` és `RestartSec=300` beállítással próbálkozik újra. A hoston ellenőrzött systemd 259.5 helyi `systemd.service(5)` kézikönyve szerint a `Type=oneshot` mellett az `on-failure` engedett; az `always` és `on-success` tiltott. A sikeresen lefutott, akár teljesen csendes audit nem indul újra magától.

A `[Unit]` rész `StartLimitBurst=3` és `StartLimitIntervalSec=3600` értéket kapott. A három az első indítást is tartalmazza, tehát az egyórás ablakban legfeljebb két automatikus újrapróbálás fér bele. Az egyórás ablak azért fontos, mert a rövid alapértelmezett rate-limit ablak a 300 másodperces restartok között már lejárna, és tartós hiba esetén nem fogná meg a ciklust. A 180 másodperces futási timeouttal együtt is belefér a három indulás az egy órába. A limit elérése után nem garantált az automatikus újrakezdés pusztán az ablak lejártával: új timerindítás vagy operátori beavatkozás kellhet.

Az `OnFailure=marveen-audit-failure@%n.service` továbbra is KOMMENT. Ezt csak egy ténylegesen elkészített, önálló értesítési csatornát használó operátori fallback unithoz szabad bekapcsolni. Tartós dashboardhiba esetén a best-effort üzenet sem jut el BELA-hoz, és a hiba csak a journalban látszana. Ilyen fallback unitot nem készítettem/aktiváltam. A szintaktikai naptárellenőrzés igazolja: 2026-10-19 hétfő 09:00 CEST és 2026-10-26 hétfő 09:00 CET. A restartkombinációt a telepített 259-es kézikönyvből és a sablon tartalmát olvasó automatizált tesztből ellenőriztem. Teljes unit-verify, éles restart/systemd/WSL futás, timertelepítés vagy engedélyezés nem történt.

Telepítési sorrend, kizárólag későbbi operátori végrehajtásra:

1. Tiltsd le a régi `heti-elakadas-audit` heartbeatet, és ellenőrizd, hogy nincs belőle futó forduló.
2. Alkalmazd a patch-et, töltsd ki a service két repoútját. Ellenőrizd a helyi desired fájlt, a kanban-audit segédfájlt és az API elérhetőségét. A token tartalmát ne másold a terminálra.
3. Futtasd a `--seed-reported --dry-run` előnézetet, majd egyszer a `--seed-reported` feltöltést. Ez nem teljes audit, és nem hamisít log-kurzorállást.
4. Másold a kitöltött unitokat a user systemd könyvtárba, végezz `daemon-reload` és helyi unit-ellenőrzést, majd engedélyezd a timert: `systemctl --user enable --now heti-elakadas-audit.timer`. Persistent timer esetén akár rögtön jöhet felzárkózó futás.
5. Az első tényleges futás után nézd meg: `journalctl --user -u heti-elakadas-audit.service --since today --no-pager`. Ellenőrizd a JSON eredményt, a desired-forrást, a log kezdeti kurzorát és hogy a régi heartbeat továbbra is tiltott. Tartós hiba vagy start-limit után a javítást követően operátori `reset-failed` és új indítás lehet szükséges.

## Tesztek és külön kritikus stresszkör

```text
python3 -B scripts/__tests__/heti-elakadas-audit.test.py
Ran 101 tests in 1.734s
OK
python3 -B scripts/__tests__/kanban-audit.test.py
Ran 60 tests in 0.774s
OK
```

Mindkét csomag izolált DB-t, naplót, állapotot és mockolt dashboardot használ. A valódi DB-nek csak a sémáját olvastam ki. Nem történt éles GET/POST, hozzárendelés-javítás, éles állapotmentés, seed, commit, push vagy dashboard-újraindítás.

A 46 alapteszt után végzett külön stresszkör találatai és javításai:

1. **Csonkolás után már visszanőtt napló:** az inode és méret önmagában nem elég. Rövid, csak hash-ként tárolt ellenőrzőpont teszi felismerhetővé a tipikus copytruncate-visszanövést, `rewritten` állapottal.
2. **Hibás, de nem negatív offset:** a felismerő mindig a snapshot végéig feldolgoz. Ezért a log-state csak offset==size esetén érvényes, nem enged látszólag érvényes, visszafelé mutató kurzort.
3. **Új alágens admin-feladata:** a kezdeti fix subagent-lista nem látta volna az API-ban már létező új ágenst. A hitelesített API-lista neveit is bevonja, BELA és István kivételével; az egyszerű kulcsszólista továbbra is konfigurálható.
4. **Fél sor és ANSI miatti elveszett/dupla számlálás:** véges állapot és soronként egy találat védi, marker-, CSI- és OSC-határon át is, nyers carry-szöveg nélkül. Húsz véletlen darabolással is egyezik a referencia. Sérült UTF-8, félbemaradt ANSI, forgatás közben maradt marker külön tesztelt.
5. **Verseny a naplóíróval:** scan közbeni append a következő auditig marad; forgatás/csonkolás észlelésekor hiba és változatlan tartós kurzor. Valódi két auditfolyamat egyetlen üzenetet küldött. FIFO és symlink napló nem blokkol és nem olvas titkos másik fájlt.
6. **Leállás vagy lemezhiba:** a tartós állapot változatlan marad sikertelen replace-nél, nincs tmp-szemét, a következő sikeres futás ismét látja ugyanazt a kártyát és lognövekményt. A nyers hibaszöveg akkor sem kerül át, ha szimulált tokent tartalmaz.
7. **Nagy napló:** a kezdeti 16 MB payloadból 0 bájt scan történik. Külön, sűrű soros benchmark 16 773 309 új bájton 4207 helyes találatot számolt 3,866 másodperc alatt; a logállapot 273 bájt. A hosszú, 16 MB-os egyetlen sor tesztje is sikeres, a memória chunkmérethez kötött, nem a sorhosszhoz. Az idő platformfüggő, nem szolgáltatási garancia.
8. **Kimenet és időzóna:** a token, raw log, ANSI és kártyaszöveg nem jelenik meg a cron/journal összefoglalóban. A címek csak a méretkorlátos, idézett BELA-üzenetbe kerülnek. DST epoch-egyezés, hiányzó/hibás DB és API-adat, hibás cache, kétórás, 50-es, 30 000-es és 28 napos határ tesztelt.

Megmaradó korlát: a DB, log és ágenslista nem közös időpillanatból származik; BELA beavatkozás előtt ellenőrizze az aktuális helyzetet. A kulcsszavas assignee-ellenőrzés jelentést nem értelmez. Hibás/importálhatatlan vagy hiányzó kanban-audit segédfájl a script indulását megakadályozza, ekkor is a későbbi OnFailure fallback és journal adhat külső jelzést. Éles kézbesítés és a service futási környezete ebben a sandboxban nem volt ellenőrizve.


## BELA review utáni zajcsökkentés és megismételt stresszkör

A régi, névre/várakozásra kereső unassigned szabályt megszüntettem. Mind az öt megadott cím negatív regressziós fixture, még Istvánt vagy ágenseket említő leírás mellett is. A mesterséges „WP admin felulet beallitas jovahagyas” pozitív. Minden kért kulcsszó ékezetes/ékezet nélküli, kis/nagybetűs, title/description változata tesztelt. A b ág továbbra is csak az alágens címét nézi, és ugyanaz a 28 napos korlát védi.

Külön elsőfutás-tesztben 17 általános, névvel/várakozással leírt, gazdátlan kártya és 4000 régi logsor mellett: hygiene_count=0, log_delta=0, log_change=initialized, notification_needed=false, nincs küldés. Egy új adminisztrációs találat ugyanakkor az első normál auditban is egy üzenetet eredményez. Ez izolált fixture, nem az éles DB újramérése.

A review utáni saját kritikus kör két további zajforrást javított: a cím vége és leírás eleje külön-külön nem tartalmazta az admin-kulcsszót, de összefűzve tévesen létrehozhatta; most mezőnként keres. Egy véletlenül üres konfigurációs kulcsszó pedig az összes gazdátlanra illeszkedett volna; ezt explicit hiba védi, seed módban üzenet nélkül.

A seednél célzottan tesztelt: két ismétlés nem tolja tovább a némítást, más jelentés megmarad, lejárt találat új alapidőt kap, nulla és részleges logállapot változatlan, az audit-időpont változatlan, azonos időpontnál is működik, hiba közben nincs best-effort POST, disk-full/replace után a régi state épen marad és újrapróbálható. A korábbi valódi folyamatközi lockteszt seed alatt is busy állapotot igazol. A következő normál audit feldolgozza a seed közben beérkezett logsorokat és új kártyákat; az egyéb retry/életjel kivételek sem vesznek el. A teljes korábbi log-, UTF-8-, ANSI-, race-, DST- és titokmentességi csomag ismét sikeres.

Az átadás a frissített `heti-elakadas-audit.patch`, a jelenlegi marveen fájlokhoz mérve. A már beépített kanban-audit változatlan. Éles DB-n, éles agents API-val vagy éles seeddel nem végeztem futáspróbát. Telepítés és aktiválás nem történt; a korábban leírt kézbesítési, logforgatási és systemd-ellenőrzési korlátok megmaradnak.


## KISPROGI telepítés előtti hárompontos javítás

A küszöb most 30 000, a mért 8000–14 000 heti alapszint nem riaszt. A konkrét 30 000/30 001 határ és a nyolc-/tizennégyezres növekmény tesztelt. A desired-lista tényleges tömbformátumát a célrepo kódjában és adatfájljában ellenőriztem. Teszt fedi a hiányzó, üres, sérült, túl mélyen beágyazott, hibás UTF-8, nem lista, hibás elem, duplikátum, új/missing ágens, tulajdonosnevek és hozzáférési hiba eseteit, valamint a nem desired on-demand ágenseket és a listától független reauth-jelzést. Dry runban a desired-fájlt sem írja át, a seed is megőrzi korábbi csendes szemantikáját.

A saját újabb stresszkör fő találata: a puszta StartLimitBurst=3 rövid alapértelmezett ablakkal nem korlátozná az 5 percenkénti újraindítást. Ezért került mellé az explicit egyórás ablak, helyesen a Unit szekcióba. A sablont teszt igazolja: három indulás és maximális futásidők is beleférnek az ablakba, az OnFailure komment marad. A desired-alapú vizsgálat kötelező paramétert kap, hogy egy későbbi hívó ne térhessen vissza véletlenül a fix listás működéshez. A fallback nem nullázza a hygiene- vagy logállapotot, és a hibás lista tartalma nem kerül a kimenetbe.
