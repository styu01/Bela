# Modell nélküli kanban-audit

A szkript a kanbant olvassa, és csak kivétel esetén ad munkát BELA-nak. Nem használ modellt, nem archivál, nem pingeli a felelősöket, nem ír kártyát vagy kommentet, és nem küld Telegramot. Az autonómia-konfigurációt nem módosítja és magasabb szint esetén sem végez több műveletet.

Az első sikeres futás csak beállítja az audit időpontját. Ezután egyetlen helyi dashboard-üzenetet küld BELA-nak, ha van az előző audit óta változatlan, érvényes felelőshöz rendelt, hét napja nem jelentett `in_progress` kártya, új, 48 óránál régebbi `waiting` blokker, vagy húsznál több archiválásra váró kártya. A gazdátlanok száma önmagában nem vált ki jelzést, más jelzés végére kerül. A beakadt és új blokker összesített hármas küszöbe is teljesülhet, de már egyetlen beakadt vagy új blokker is elég.

A done kártyák közül csak a szigorúan hét napnál régebbieket számolja. Pontosan hét nap még nem archiválási jelölt, pontosan 48 óra még nem régi blokker. A beakadás alapja `updated_at < last_audit_at`, nem egy rögzített 24 órás kor. Csak kitöltött felelőssel számít beakadtnak; az üres, whitespace, `bela` és `istvan` felelős kimarad. A két név ellenőrzése kis- és nagybetűtől független, a szélső szóközöket levágja.

A `store/kanban-audit-state.json` a `last_audit_at` és `reported` mezőket tartalmazza. Az utóbbi kártyaazonosítónként tárolja az utolsó sikeres jelentés időpontját. A waiting blokkerre és a beakadt kártyára egyaránt hét napos újrajelentési korlát vonatkozik, kártyánként. A hetedik nap pontos leteltével ismét jelentheti, ha továbbra is megfelel a feltételeknek. A `stuck_count` és `new_blocker_count` a korláton átjutó, most jelenthető kártyák száma. A húsz fölötti archiválási hátralék továbbra is minden új auditban jelezhet. Azonos `--now` időpontú normál ismétlés nem küld újra.

Hiányzó vagy sérült állapotfájl új első futásnak számít. Olvasási jogosultsághiba esetén viszont hibával leáll, nehogy egy hozzáférési gond elveszítse a korábbi állapotot. Hibás vagy a futás jelen idejénél jövőbelibb jelentési bejegyzést figyelmen kívül hagy. A seed miatt az audit időhatáránál újabb, de nem jövőbeli jelentési időpont érvényes. Visszafelé mozdult óra esetén nem küld és nem ír, hibát jelez.

Az üzenet ékezet nélküli magyar szöveg. A cím legfeljebb 60 karakter, idézett adatként szerepel. BELA-t kifejezetten kéri, hogy pingelés előtt végezze el a memória-ellentmondás ellenőrzését: `GET /api/memories?agent=bela&q=<cim>`. Ezt a szkript maga nem hívja. Egy üzenetben legfeljebb 24 kártya részlete szerepel, a többi darabszámmal és a teljes helyi lista ellenőrzésének kérésével. A jelentett waiting és beakadt halmaz ilyen összesítésnél is megkapja a hét napos újrajelentési védelmet.

## Használat és kimenet

```text
python3 -B scripts/kanban-audit.py --dry-run
python3 -B scripts/kanban-audit.py --state-dir /tmp/teszt-store --db /tmp/teszt.db --now 2026-11-05T08:00:00+01:00 --dry-run
```

### Csendes induló feltöltés

Telepítéskor a régi waiting hátralékot egyszer csendesen fel lehet venni a már jelentettek közé:

```text
/usr/bin/python3 /path/to/marveen/scripts/kanban-audit.py --seed-reported --dry-run
/usr/bin/python3 /path/to/marveen/scripts/kanban-audit.py --seed-reported
```

Ezek mintaparancsok, az éles seedet nem futtattam le. A `--seed-reported` csak a nem archivált, szigorúan 48 óránál régebbi waiting kártyákat veszi fel a `reported` mezőbe, a futás időpontjával. Nem küld üzenetet és nem módosítja a `last_audit_at` értékét. Az előző hét napban már felvett azonosító időpontját sem tolja tovább: két közeli futtatás byte-ra azonos állapotot hagy, és a második nem ír újra. Lejárt bejegyzés új seednél új alapidőt kap. Más kártyák meglévő jelentéseit megőrzi. A védelem hét napos, nem végleges felmentés.

A seed ugyanazt a flockot és atomikus mentést használja, mint a normál audit. Dry run mellett nem ír, nem küld és lockfájlt sem hoz létre. Ha még nincs audit-időpont, null marad; az első későbbi normál audit inicializál, és megőrzi a seedelt jelentéseket. Az audit idejével azonos időpontban is szabad seedelni. Visszafelé mozdult óra esetén viszont ez a mód is hibával megáll.

A `--now` Unix-másodpercet vagy időzónával megadott ISO-időt fogad. A `--db` az alapértelmezett `store/claudeclaw.db` helyett másik fájlt olvas. Egy adatbázishoz egy közös állapotkönyvtárat használj. A `--dashboard-url` alapértéke `http://localhost:3420`; másik helyi port, `127.0.0.1` vagy `[::1]` is megadható, külső cím nem. Nem enged hitelesítési adatot, útvonalat, queryt vagy fragmentet az alap-URL-ben.

A service `WorkingDirectory=/path/to/marveen` beállítása mellett a relatívan megadott `--state-dir store` és `--db store/claudeclaw.db` a repo alá mutat. A token mindig a választott állapotkönyvtár `.dashboard-token` fájlja; az állapotfájl is ugyanott van. Kapcsolók nélkül az alapérték még erősebb: a szkript saját fájlhelyéből számított abszolút `<repo>/store` út, így eltérő cwd sem viszi más adatbázishoz. A service `/usr/bin/python3` abszolút futtatható fájlt használ, PATH-tól nem függ. Izolált másolattal, üres PATH mellett, a service cwd-jéből és más cwd-ből is sikeres a futás; a relatív state/DB és token út külön ellenőrzött.

A dry run sem állapotot, sem lockot nem hoz létre, és nem küld hálózati kérést. Az adatbázist `mode=ro&immutable=1` módban olvassa, hogy SQLite segédfájlokat se írjon. Nem üres WAL esetén hibával leáll: checkpointolt másolaton használd az előnézetet, az éles adatbázison a szkript nem végez checkpointot. A normál futás `mode=ro` és `query_only` módban látja a WAL-ban lévő friss adatokat is. A SQLite saját WAL/SHM olvasási könyvelése nem kártyaírás; a fő adatbázis tartalmát nem változtatja.

A stdout mindig egy JSON-sor, benne a darabszámokkal, küldési eredménnyel vagy fix hibakóddal. Hiba esetén a stderr is csak fix hibakódot kap, a kilépési kód 1. Nincs külön naplófájl; a service journalba irányítja a kimenetet. Token, kártyacím, HTTP-választörzs és nyers kivétel nem kerül a naplóba. A bearer tokent a `store/.dashboard-token` fájlból olvassa; nem nyúl a `.env` fájlhoz.

Dashboardhiba esetén sem a `reported`, sem a `last_audit_at` nem frissül, tehát a következő futás újrapróbálhat. Sikeres sorba állítás vagy értesítést nem igénylő audit után mindkettő atomikusan mentődik. A tényleges API 200-as választ és üzenetazonosítót ad: ez a sorba állítás nyugtája, nem a BELA általi feldolgozás igazolása. A későbbi kézbesítést a router kezeli, a szkript nem követi.

## Időzítősablon

A `scripts/systemd/kanban-audit.service` és `.timer` user-unit sablon. A service két `/path/to/marveen` helyőrzőjét ki kell cserélni. A timer naponta 08:00, 12:00, 16:00 és 20:00 Europe/Budapest időpontban fut, `Persistent=true` beállítással. Átálláskor előbb a régi heartbeatet kell letiltani, hogy ne fussanak együtt. A sablonokat nem telepítettem és nem engedélyeztem.

A naptárkifejezés `systemd-analyze calendar` ellenőrzése sikeres, az október 25-i óraátállás utáni négy időpont 08:00/12:00/16:00/20:00 CET. A teljes `systemd-analyze --user verify` a sandbox socketkorlátozása miatt nem fejeződött be sikeresen; a sandboxon kívüli ellenőrzés engedélykérését elutasították. Ezért nincs igazolt teljes unit-verify vagy éles systemd/WSL futáspróba.

## Ellenőrzött eredmény és kritikus stresszkör

```text
python3 -B scripts/__tests__/kanban-audit.test.py
Ran 60 tests in 0.767s
OK
```

A tesztek ideiglenes SQLite-adatbázist és mockolt küldést használnak. Az oszlopok és státuszkorlátok a jelenlegi kanban sémáját követik. Az éles adatbázist és API-t nem használtam futáspróbára.

Az alaptesztek utáni külön kritikus kör találatai és javításai:

1. Egy tetszőleges truthy `id`, például objektum vagy boolean, korábban elfogadható API-nyugta lett volna. Csak pozitív egész vagy legfeljebb 128 karakteres nem üres sztring azonosító érvényes.
2. Jövőbeli `reported` időpont tartósan elnémíthatta volna a blokkert. A futás jelen idejénél újabb időpontot az olvasó eldobja; a seed által létrehozott, auditnál újabb, de már megtörtént időpontot megőrzi. Jövőbeli audit-időhatár esetén az óra-visszalépés látható hiba.
3. A zárolási útvonal FIFO vagy symlink lehet. Nem blokkoló, symlinket nem követő megnyitás és normálfájl-ellenőrzés védi. A FIFO és a folyamatközi flock valódi izolált gyermekfolyamatban tesztelt.
4. Részleges fájlírás nem maradhat kész állapotként. A mentés 0600-as egyedi tmp fájl, fsync, atomikus replace és könyvtár-fsync. Replace-hibánál a régi állapot megmarad, a tmp törlődik. A könyvtár-fsync hibája is fix hibakóddal látható.
5. A DB-kapcsolat írásmentességét valódi sikertelen UPDATE-kísérlet bizonyítja `mode=ro` mellett. A futás előtt és után a teljes fő DB bájtjai és a kártyák egyeznek, komment nem keletkezik. Aktív módban valódi WAL-frissítés látszik, dry runban a WAL-eset írásmentesen visszautasított.
6. Az idézett kártyaszöveg nem kerül shellbe vagy parancssorba. Kontrollkarakterek és tiltott gondolatjelek kiszűrve, az üzenet mérete korlátos. SQL- és shellszövegnek látszó adat, 200 kártyás lista, hibás epoch, üres tábla és hiányzó DB külön tesztelt.
7. Proxy vagy átirányítás kiszivárogtathatná a tokent. A transport nem használ környezeti proxyt és nem követ redirectet; külső dashboard-URL eleve tiltott. A HTTP-hibaválaszt lezárja, tartalmát nem naplózza. Titkot tartalmazó szimulált kivétel sem jelenik meg a stdout/stderr kimenetben.

Megmaradó korlát: a távoli POST elfogadása és a helyi állapot mentése nem közös tranzakció. A kettő közti leállás vagy lemezhiba után egy üzenet ismétlődhet. A flock az egyidejű futást védi, de ehhez a router oldalán kellene idempotenciakulcs a teljes kiküszöböléshez. A DB-pillanatkép és a POST között egy dashboardfelhasználó megváltoztathatja a kártyát, ezért BELA-nak beavatkozás előtt az aktuális helyzetet is ellenőriznie kell.


## BELA review utáni javítás és új kritikus stresszkör

A felelős nélküli, BELA-hoz vagy Istvánhoz rendelt folyamatos projektek már nem riasztanak beakadásként. A hét napos korlát mindkét jelentett kártyatípusra érvényes. A review számaival felépített izolált regressziós adatbázisban seed után: archive_count=8, stuck_count=0, new_blocker_count=0, unowned_count=19, notification_needed=false. Ez tesztfixture, nem új éles DB-mérés.

Az új kritikus kör két valódi állapotkezelési ütközést javított: a régi olvasó az auditnál újabb seedelt időpontokat eldobta volna, az első normál inicializálás pedig üresre cserélte volna a seedelt jelentéseket. Most csak a tényleges jövőbeli időpont hibás, a null audit-időpont mellett is megmaradnak a seedelt adatok. További védelem, hogy a seedet az azonos audit-időpont nem nyomja el, a megismételt seed nem tolja ki folyton a némítás határát, és az atomikus írás hibája nem veszít el régi jelentéseket. Külön teszt fedi a pontos 48 órás és hét napos határt, az új waiting kártyát, a meglévő jelentések megőrzését, a párhuzamos seed zárolását, valamint a hálózat és token nélküli működését.

A változások továbbra is kizárólag a munkatérben készültek. A frissített `kanban-audit.patch` a jelenlegi `/home/kisss/marveen` állapotából készül. Éles seed, értesítés, telepítés, aktiválás, commit és push nem történt. A korábban jelzett API-elfogadás és helyi mentés közötti ismétlődési lehetőség változatlan.
