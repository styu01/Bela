# Reggeli kutatás kiküldése modell nélkül

A `scripts/reggeli-kutatas-kikuldes.py` az 05:30-as előkészítő heartbeat egyetlen gépies lépését váltja ki: a megadott, változatlan kutatási sablont elküldi `bela` feladótól a `codex/programozo` címzettnek a helyi `POST /api/messages` útvonalon. Nem kutat, nem használ modellt és nem küld Telegramot. A sablonban csak a `{YESTERDAY}` helyére kerül dátum; a keresőkifejezések rögzített 2026-os évszáma is változatlan.

**A 06:00-as napindító továbbra is Claude-dal végzi a „codex válasz kikeresése” lépést. Ez a változás azt nem módosítja.**

A tegnapot Europe/Budapest szerinti naptári napként számolja, nem 86 400 másodperc levonásával. Így az óraátállás, évváltás és szökőnap is helyes. Az állapot a `store/reggeli-kutatas-state.json`: `{"last_sent_date":"YYYY-MM-DD","message_id":123}`. A dátum a küldés budapesti napja. Csak sikeres HTTP 200 és pozitív, valódi egész üzenetazonosító után ment, 0600-as tmp fájl, fsync, atomikus replace és könyvtár-fsync használatával. A `bool`, sztring és törtszám nem nyugta.

Azonos napon újabb futás `already_sent` eredményt ad, hálózati hívás nélkül. A `--force` ezt a napi védelmet feloldja, de a reggeli időhatárt nem. Hiányzó/sérült állapotfájl új küldést tesz lehetővé; jogosultsági/olvasási hiba viszont látható hibát ad. Ha a tárolt dátum a jelen napnál későbbi, `clock_rollback` hiba keletkezik force mellett is, és a state változatlan marad.

## Használat

```text
/usr/bin/python3 scripts/reggeli-kutatas-kikuldes.py --dry-run
/usr/bin/python3 scripts/reggeli-kutatas-kikuldes.py --state-dir /tmp/reggeli-preview --now 2026-11-05T05:30:00+01:00 --dry-run
```

A `--now` Unix-másodperc vagy időzónás ISO-idő. Tesztóraként a dátumot és a késési ellenőrzést is felülírja. A `--dry-run` nem küld, nem hoz létre state-dir/lock/bytecode fájlt, és nem ír állapotot. Ha lenne küldés, a JSON-ban a teljes pontos sablon előnézete is szerepel. A fenti minták itt nem az éles állapoton végrehajtott küldések.

Kapcsolók nélkül a szkript saját repohelyéből veszi a `<repo>/store` könyvtárat, más cwd sem viszi máshová. A `--state-dir` a token és state könyvtára is; relatív út a service WorkingDirectory-jából értendő. A `--dashboard-url` alapértéke `http://localhost:3420`; csak localhost/127.0.0.1/::1 HTTP-cím és helyi port engedett. A bearer tokent a `.dashboard-token` fájlból olvassa, nincs shell, külső hálózat, proxy vagy átirányítás. A már telepített `scripts/kanban-audit.py` közös mentési, dátum- és transport-beállítási segédeit használja. Saját POST-adapter kell a külön címzett és az egész nyugtaazonosító megőrzéséhez.

A teljes aktív futást flock védi. Párhuzamos második példány `busy` eredményt ad. A stdout egyetlen JSON-sor; hibánál a stderr csak fix hibakód, a kilépési kód 1. Token, tokenes URL, HTTP-törzs és nyers kivétel nem kerül a kimenetbe. Hálózati/nyugtahiba esetén a state nem frissül, ezért a következő service-próba újrapróbálhat.

## Időzítés és átállás

A user-unit sablonok `scripts/systemd/reggeli-kutatas-kikuldes.service` és `.timer`. Naponta 05:30 Europe/Budapest, `Persistent=false`, nincs véletlen késleltetés. A kihagyott naptáreseményt a timer nem pótolja utólag. A service oneshot, `Restart=on-failure`, `RestartSec=120`, `StartLimitIntervalSec=900`, `StartLimitBurst=3`, `TimeoutStartSec=30`. Ez az első indulással együtt legfeljebb három indulás a 15 perces ablakban. A host systemd 259 kézikönyvében korábban ellenőrzött oneshot/on-failure kombinációt követi.

A script plusz védelme: budapesti 06:00-tól csendes `expired`, exit 0, POST és állapotmentés nélkül, így nem indul végtelen retry. Valós óra esetén a lock megszerzése után és közvetlenül a POST előtt is ellenőrzi a dátumot/időhatárt: lassú állapotolvasás vagy felfüggesztés miatt nem indíthat elavult napra kérést. Előrejelzett tesztóránál a megadott `--now` érvényes. A script a nap elején kézzel korábban is futtatható, a timer ütemezése továbbra is 05:30.

Későbbi operátori telepítési sorrend:

1. **A régi `reggeli-napindito-codex-elokeszit` heartbeatet a timer engedélyezése ELŐTT le kell tiltani.** Ellenőrizd, hogy már nem fut korábbi forduló.
2. Alkalmazd a patch-et, töltsd ki a service két `/path/to/marveen` útját. A közös kanban-audit segédfájl legyen jelen.
3. Ellenőrizd a dry run előnézetét. A tokent ne írasd ki és ne tedd parancssorba. Tesztóra és force mellett se végezz próbaküldést az éles állapoton.
4. Másold a kitöltött sablonokat a user systemd könyvtárba, ellenőrizd a unitokat, végezz daemon-reloadot, és csak ezután engedélyezd az új timert. Az első tényleges futás után ellenőrizd a `journalctl --user -u reggeli-kutatas-kikuldes.service` kimenetét és az állapot napját/nyugtáját.

A naptárellenőrzés helyes 2026-10-24 05:30 CEST és 2026-10-25 05:30 CET időpontot adott. Teljes unit-verify, service-újraindítási próba vagy éles systemd/WSL futás nem történt. A sablonokat nem telepítettem és nem engedélyeztem.

## Tesztek és saját kritikus stresszkör

```text
python3 -B scripts/__tests__/reggeli-kutatas-kikuldes.test.py
Ran 45 tests in 0.465s
OK
python3 -B scripts/__tests__/kanban-audit.test.py
Ran 60 tests in 0.721s
OK
```

A tesztek izolált állapotkönyvtárat, hamis tokent és mockolt HTTP-t használnak. A beépített kanban-audit és heti-elakadas-audit mintafájlokat összevetettem a célrepóval; egyeztek, nem módosítottam őket. Éles API-kérés vagy üzenetküldés nem történt.

A 35 alapteszt utáni külön kritikus kör eredménye:

* **Késve ébredő/sokáig állapotot olvasó service:** a Persistent=false önmagában nem védi a kézi vagy felfüggesztett indítást. A kliens közvetlenül a POST előtt újra ellenőrzi a valódi órát, napváltás vagy 06:00 után eldobja az elavult kérést. Force sem küldi későn. A hálózati elfogadás és a címzett tényleges feldolgozásának időpontja ettől még nem garantált.
* **Napi duplázás és race:** két valódi dispatcher-folyamat egyszerre összesen egy kérést küldött. Száz ismételt reggeli futás sem ismétel. FIFO/symlink lock nem blokkol és nem módosít célfájlt.
* **Nyugtahamisítás és rossz input:** boolean/sztring/tört/negatív/nulla nyugta nem véglegesíthet küldést. Sérült, mélyen beágyazott JSON, érvénytelen naptári nap, jövőbeli state és időzóna nélküli tesztidő külön ellenőrzött.
* **Íráshiba:** replace-hibánál a régi state ép marad és nincs tmp-szemét. A már sikerült rename utáni directory-fsync hibánál a state továbbra is érvényes JSON; következő futás nem küld újra, ha a nyugta megmaradt.
* **Dátum és sablon:** független, szó szerinti elvárt sablon ellenőrzi a teljes payloadot, a pontos címzettet, az ASCII szöveget és a tiltott gondolatjelek hiányát. DST, évvége, szökőnap és a UTC-től eltérő budapesti dátum tesztelt. A naptári tegnap számítása a tavaszi 23 órás napnál is helyes.
* **Titokszivárgás:** tokent/URL-t tartalmazó szimulált kivétel is csak fix kódot ad. Külső dashboard, proxy és redirect tiltott. A helper betöltése és a dry run üres PATH/eltérő cwd mellett sem hoz létre bytecode- vagy state-fájlt.

Megmaradó korlát: a távoli elfogadás és a helyi state mentése nem közös tranzakció. A POST sikerét követő processzhalál vagy sikertelen állapotmentés után az újrapróbálás megismételheti az üzenetet. Ennek teljes kiküszöböléséhez fogadóoldali idempotenciakulcs szükséges; a követelmény szerint a state csak sikeres nyugta után íródik. A megőrzött egész id sorba állítást igazol, nem garantálja a federation későbbi kézbesítését vagy a kutatás elkészültét. A hiányzó/sérült közös segédfájl indulási hiba, ezért telepítéskor ellenőrizni kell. Nem történt commit, push, dashboard-újraindítás vagy régi scheduled task módosítás.
