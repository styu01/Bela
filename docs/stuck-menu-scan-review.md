# Modell nélküli flottaszintű menükereső, 2b/6 felülvizsgálat

A 2026. november 4-i KISPROGI review 1–6. pontjának javítása. Az új flottamód továbbra is alapértelmezetten shadow, az eredeti fő-session modal-guard működése nem változik.

## Átadandó változások és alap

Ebben a körben három fájl változott:

* `scripts/stuck-modal-guard-fleet.py`;
* `scripts/__tests__/stuck-modal-guard-fleet.test.py`;
* `docs/stuck-menu-scan-review.md`.

A már átvett `scripts/stuck-modal-guard.sh` `fleet` belépési pontja és a `scripts/systemd/stuck-menu-scan.service`/`.timer` sablonok most nem módosultak. Nem készült második önállóan ütemezett guard: a Python modul a meglévő shell guard flottamódjának implementációja.

A változások a kijelölt `/home/kisss/codex-review-marveen` munkatérben készültek. A `/home/kisss/marveen` a jelenlegi sandboxból nem írható. A kiinduló Python modul, teszt és dokumentáció egyezett a BELA által már átvett marveen példánnyal. A `stuck-menu-scan-2b.patch` a **jelenlegi `/home/kisss/marveen` állapothoz képest** készül, nem a 2/6 előtti alaphoz. A célrepo nem lett módosítva.

## Használat és alapmód

```bash
bash scripts/stuck-modal-guard.sh fleet
bash scripts/stuck-modal-guard.sh fleet --dry-run
```

Az első parancs alapból shadow: csak olvas és strukturális megfigyelési rekordokat naplóz. Nem küld billentyűt, ágensüzenetet vagy Telegramot. A `--dry-run` még fájlt, lockot és naplót sem ír. Az aktív mód kizárólag explicit `--mode active` kapcsolóval választható, nincs automatikus átállás.

További kapcsolók: `--state-dir`, `--now` (epoch másodperc vagy időzónás ISO), `--sessions` (vesszővel elválasztott pontos sessionnevek). A flotta egyetlen alapkonfigurációs helye a modul `SESSIONS` konstansa, most hét sessionnel:

`agent-progi`, `agent-okoska`, `agent-kisprogi`, `agent-iris`, `agent-vizsla`, `agent-autobot`, **`agent-deepseek`**.

A háromnapos régi/új shadow összehasonlítás és az egyezések operátori ellenőrzése továbbra is kötelező az aktívra váltás előtt. Ezt a javítás nem végzi el és nem helyettesíti.

## Hiányzó session, tmux 3.6

A pontos sessioncél továbbra is `=agent-NAME:`. A `display-message` szokásos hiányzó-session hibáján túl a tmux 3.6 sajátos válasza is kezelt: **rc=0, hat üres mező, `|||||` kimenet**. Ez `session_absent`, `class=none`, nem hibarekord. Az üres és a whitespace-t tartalmazó üresmezős válasz is hiányzó session.

A részben kitöltött, hibás metadata továbbra is hiba: abból nem szabad érvényes pane ID-t vagy hiányzó sessiont találgatni. A hiányzó session nem indít capture-t, billentyűt vagy értesítést. A tesztcsomag a ténylegesen jelentett rc=0/üresmezős viselkedés fixture-jével a CLI exit 0 eredményét is ellenőrzi, nem csak a régi rc=1 mockot.

## Ismert menü: szükséges bizonyítékok

A régi szigorú szabályok megmaradtak: pontos `What do you want to do?` kérdés; egyetlen, egyértelműen számozott `Stop and wait for limit to reset`; legfeljebb egy opcionális `Upgrade your plan`; különböző 1–9 közötti ASCII számjegyek; csak ismert navigációs footer. Nincs upgrade-választás. A dupla opció, vezető nulla, többjegyű szám, tördelt címke, további utasítás, code fence vagy terminálvezérlő szekvencia nem automatikusan kezelhető eset.

Most ezen felül **legalább az egyik** kell:

* az adott kérdés utáni valamelyik számozott opcióban `❯` vagy `›` kiválasztókurzor;
* a kérdés előtt `session limit` vagy `hit your` fejlécjelző.

A sima szöveg, például a kérdés, a két számozott opció és az `Enter to confirm` fejléc és kurzor nélkül **unknown**, `missing_selection_evidence` indokkal. Egy korábbi felsorolás kurzora a kérdés előtt nem bizonyítja a mostani menü kijelölését.

Billentyű előtt a pane ID, PID, session és előtérparancs ismét ellenőrződik, és új capture készül. A második kép döntési aláírása a besorolást, stop-sorszámot és strukturált opciókat ellenőrzi. A teljes tartalom hash-e szándékosan nem része ennek: egy fejlécben változó visszaszámlálás nem tehet egy változatlan menüt állandóan beavatkozhatatlanná.

Csak nem copy-mode-ban lévő, élő, `claude`, `claude.exe` vagy `node` előtérparancsú pane kaphat számjegyet. A parancs shell nélküli argumentumlista, egyetlen ellenőrzött számjeggyel és Enterrel. A pane szövege nem jut parancssori argumentumba, naplóba vagy külső üzenetbe.

## Strukturális shadow ujjlenyomat

Shadow napló: `store/logs/stuck-menu-guard-shadow.jsonl`. Aktív napló: `store/logs/stuck-menu-guard.jsonl`. Minden megfigyelt ablak rekordja tartalmazza az időpontot, ablakot, osztályt, indokot, tervezett és végrehajtott műveleteket. A `fingerprint` mező kizárólag strukturális adatot tartalmaz:

* `line_count`: a vizsgált legfeljebb 15 sor száma;
* `question_count`: pontos kérdéssorok száma;
* `cursor_option`: látszik-e kiválasztókurzor számozott opción bárhol a képen;
* `cursor_in_menu`: a kérdés utáni opcióhoz tartozik-e kurzor;
* `header_line`: látszik-e fejlécjelző bárhol;
* `header_before_question`: a fejléc a kérdés előtt van-e;
* `options`: opciószám, `stop`/`upgrade`/`other` típus és kurzorjelző; az opció felirata nincs benne. A két számjegynél hosszabb szám `multi_digit`, nem korlátlan pane-adat;
* `healthy_footer`, `healthy_footer_types`: csak rögzített típusjelzők;
* `pane_hash`: a normalizált, utolsó 15 sor SHA-256 hash-ének első 16 hex karaktere.

Külön, a rekord tetején is szerepel a **`menu_markers_with_healthy_footer` true/false** mező. Ez közvetlenül szűrhetővé teszi a potenciális menü/footer együttállásokat a háromnapos megfigyelés alatt. Hiányzó vagy nem olvasható pane-nál a fingerprint null, a reason/error jelzi, hogy nem volt mérés.

Nincs nyers képernyőszöveg, opciófelirat, hibaüzenet, HTTP törzs vagy tokenes URL a rekordban. A hash összehasonlítási segédlet, nem eredethitelesítés és nem a képernyő visszaállításának eszköze.

## Healthy permission-módok és a hamis negatív elkerülése

A `bypass permissions on` mellett az **`accept edits on` és `plan mode on`** lábléc is ismert. A permission-módok mintája teljes footer-sorra illeszkedik, opcionális Claude UI jelzővel és zárójeles gyorsbillentyű-leírással. Egy prózában említett kifejezés vagy egy számozott opcióba írt szöveg nem válik ettől healthy footerré.

Csak az utolsó két nem üres sor állapotjelzője számít. A menü fölött megmaradt régi permission footer ezért nem rejti el a mostani menüt.

**Ha healthy footer és menüjelző együtt látszik, az eredmény unknown, nem csendes none.** Ilyenkor nincs automatikus billentyű; shadow módban az együttállás és a healthy típus naplózódik, aktív módban pedig a szokásos kétórás korlát mellett kézi ellenőrzési jelzés készül. Ez szándékos: az új permission-módok felismerése nem vezetheti be a „valódi menü eltűnt a healthy-védelem mögött” típusú néma hamis negatívot. Ha nincs menüjelző, a healthy pane normális none.

## Ablakonkénti hibaszigetelés

A scan minden ablakhoz előre létrehoz egy rekordot. A `scan_window`, a `save` és a `notify_resume` utáni mentés hibája ezen a rekordon kap rögzített hibakódot; a loop a következő ablakkal folytatódik. A már bizonyított billentyű és API-elfogadás az action listában megmarad akkor is, ha a következő állapotírás hibás.

Az audit írási hibája is ablakonként kezelt. Ha a `logs` útvonal fájl, a logfájlnév könyvtár, FIFO vagy más hibás cél, a többi ablak vizsgálata nem áll le. Az összes tisztított rekord továbbra is kijut az egysoros stdout/journal összesítésbe. Egy rekordban az `error_codes` több hibát is megőriz, az `errors` összesítő az érintett ablakokat számolja.

Állapotírási hiba után a monitor újraolvassa a **tartós** állapotot, mielőtt a következő ablak dolgozik. Így az előző ablak csak memóriában létező, nem commitolt recovery-módosítása nem kerül véletlenül mentésre a következő ablak tranzakciójával. Ha az újraolvasás sem sikerül, a további ablakok továbbra is megfigyelhetők, de aktív műveletük `blocked_state_unavailable`.

A futás előtti globális lock- vagy kezdeti aktív állapot-olvasási hiba továbbra is megakadályozhatja a teljes aktív futást: megbízható közös állapot nélkül nem szabad beavatkozni. Ez nem egyetlen ablak hibája. A hiányzó session normális eset, nem ilyen globális hiba.

## API-elfogadás és tényleges kézbesítés

A jelenlegi, hitelesített és érvényes `POST /api/messages` **200 + pending üzenetazonosító** választ ad, a cél session pillanatnyi állapotától függetlenül. Ez csak sorba állítási nyugta, nem a cél ágens tényleges fogyasztási/kézbesítési visszaigazolása. Az action neve ezért `resume_api_accepted`.

A router később próbál kézbesíteni. A hiányzó sessionhez tartozó üzenetet az egyórás retry-ablak után eldobhatja; a létező, de elfoglalt session üzenetét ez a szabály nem dobja el. A monitor a POST nyugtájából ezt nem tudja megállapítani.

A mockolt inactive válasz tesztje és a kódbeli kompatibilitási ág kizárólag védekező viselkedés egy alternatív/jövőbeli API-hoz. **Az éles API ilyen választ nem ad**, ezért ezek neve és kommentje kifejezetten mock/compatibilitási esetként jelöli őket. Nincs valós „nem futó címzett kihagyva” állítás.

## Újabb kritikus stresszteszt: találatok és javítások

1. **tmux 3.6 üresmezős siker:** a rc=0/`|||||` válasz korábban hibás pane-metadatának számított. Most normális hiányzó session. A CLI szintű regresszió hét hiányzó ablak mellett exit 0-t, errors 0-t és hibakód nélküli rekordokat igazol.
2. **Mentési és audit kivétel után megszakadó loop:** a hibák most ablakonként kezeltek; a rekord megőrzi a már megtörtént beavatkozást, és a következő ablakot is feldolgozza. Teszt van aktív elsőablakos ENOSPC-re, recovery-mentési hibára, logs fájl/könyvtár ütközésre és váratlan megfigyelési kivételre.
3. **Memóriában megváltoztatott recovery véletlen későbbi commitja:** sikertelen mentés után a tartós fájl visszaolvasása védi a következő ablak állapotírását. Teszt igazolja a reserved és notify_pending fázis megőrzését is.
4. **Sima szöveg automatikus menüként felismerése:** kurzor vagy kérdés előtti fejléc nélkül unknown. Kurzor a korábbi felsorolásban nem számít bizonyítéknak. A kurzor és a fejléc külön-külön elegendő, ha a többi szigorú feltétel is teljesül.
5. **Új hash miatt minden változó fejléc második capture-nél visszautasítva:** az ujjlenyomat hash-e nem része a beavatkozási döntés aláírásának. A menüszám és az opciók változását továbbra is tiltja, a fejléc visszaszámlálása önmagában nem akadály.
6. **Csonkolt képernyő megfigyelhetősége:** hiányzó kérdéssor mellett is külön látszik, hogy van-e kurzoros opció vagy fejléc. Ettől a besorolás nem válik automatikusan ismertté, de a shadow logból megérthető az unknown oka.
7. **Healthy mode miatti néma hamis negatív:** permission módot csak footer-sorként ismer fel, és a menü/footer együttállást unknown/manual állapotként tartja láthatóan. A korábbi footer és a prózában szereplő szavak nem nyomják el az aktuális menüt.
8. **Valódi részleges naplóírás:** egy külön gyermekfolyamatban kernel `RLIMIT_FSIZE` korláttal tényleges rövid írást/EFBIG hibát idéztem elő. A teszt hibát talált: fél JSON-sor maradt a napló végén. Javítás: egyetlen írási kísérlet, rövid írás ellenőrzése és a sikertelen sor visszavágása az előző fájlméretre, a fleet lock alatt. Mindkét ablak továbbra is rekordot ad a stdout fallbackben. A javított kernelteszt sikeres.
9. **FIFO logútvonal blokkolhatta volna a futást:** a napló nem blokkoló és symlinket nem követő megnyitást használ, és csak normál fájlra ír. Külön folyamatban FIFO-ütközéssel igazolt, hogy a scan nem ragad be és mindkét ablakra visszatér.
10. **Korábbi védelmek regressziója:** a folyamatközi lock, kétszeri pane-ellenőrzés, 10 perces key- és 2 órás alert-korlát, 1000 ismert és 1000 ismeretlen ismétlés, clock rollback, prompt-injekció, duplikált/hibás számok, kontrollkarakterek, tokenredakció és dry run tesztjei továbbra is sikeresek.

## Teljes kért tesztfuttatás

```text
python3 -B scripts/__tests__/stuck-modal-guard-fleet.test.py
Ran 73 tests in 1.138s
OK

bash scripts/__tests__/stuck-modal-guard.test.sh
Results: 19/19 passed
All tests passed.

bash -n scripts/stuck-modal-guard.sh
exit 0
```

Összesen **73 sikeres Python-tesztmódszer és 19/19 sikeres régi Bash ellenőrzés**. A kernel fájlméretkorlátja és a FIFO-teszt valódi, izolált gyermekfolyamatban fut, a két párhuzamos guard tesztje is valódi folyamatokat használ. Az ágens API, Telegram és a flotta pane-jai fake/mock bemenetek; éles ágensnek nincs billentyű vagy értesítés küldve.

## Megmaradt korlátok és nem végrehajtott műveletek

* A tmux 3.6 hiányzó-session válaszát a review-ban közölt tényleges kimenet fixture-jével teszteltem; nem indítottam éles tmux szervert, és nincs rögzített, hiteles valós Claude menüminta. Ezt a háromnapos shadow összehasonlításnak kell pótolnia.
* A tökéletesen lemásolt valódi menüt kizárólag képernyőszövegből nem lehet eredethitelesíteni. A strukturális hash nem hitelesítés. A legutolsó capture és a billentyű között a TUI változhat; a tmux nem biztosít a képernyőképpel közös atomikus beavatkozást.
* Távoli API-elfogadás és helyi nyugtamentés között bekövetkező processzhalál vagy íráshiba megismételhet egy folytató üzenetet. A tízperces billentyűkorlát ettől megmarad. A POST utáni valódi kézbesítést a guard nem figyeli.
* Ha a naplóírás visszavágása is fájlrendszerhibába ütközik, a log teljes helyreállítása nem garantálható; a tisztított rekordok a stdout/journal kimenetben maradnak meg. Más, flockot nem használó külső naplóíróval a rollback nem koordinál.
* Nem történt éles API/Telegram vagy valós fleet tmux próba, háromnapos shadow futtatás, systemd/WSL futásellenőrzés, aktívra váltás vagy régi scheduled task módosítás.
* Nem történt commit, push, dashboard-újraindítás vagy időzítő telepítés/aktiválás. Az 1b/6 fájljait ez a javítás nem érinti.
