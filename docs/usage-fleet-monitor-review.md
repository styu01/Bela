# Claude usage-küszöbök és keretnevek, 2026-11-07

István döntése alapján a Claude ötórás keret figyelmeztetése 90%, pause-határa 98%; heti 80/90 változatlan. A jelen leírás a korábbi modellmentes monitorreview működési garanciáit megtartja, a metrikafüggő döntést és az egyértelmű keretneveket vezeti be. A saját ellenőrzés után az anyag KISPROGI-review-ra készül; BELA önálló ellenőrzését nem feltételezi.

Az átadott script-, checker-, teszt-, CLAUDE.md- és sablonmódosítások külön munkamásolatban készültek: `/home/kisss/codex-review-marveen/output/usage_thresholds/`. A teljes repo-patch `usage-thresholds.patch`. A systemd sablonok és a TS állapotfájl-fogyasztók változatlan másolatok; éles futási/aktiválási állapotukat nem módosítottam és nem állítom, hogy ellenőriztem.

## A pause és a Telegram külön állapotgépe

Az állapotfájl: `store/.usage-fleet-monitor-state.json`. A `level`, `metric`, `percent`, `source`, `since` mellett tartalmazhat ágensoldali `pending` eseményt és ettől független `telegram_pending` értesítést. A teljes futást folyamatközi flock védi. Minden állapotírás egyedi, 0600 ideiglenes fájlon, fsync és atomikus rename használatával történik.

**Pause:** először tartósan menti a `pending` döntést és az értesítési tervet. Közvetlenül ezután, még bármely hálózati küldés előtt létrehozza a `store/.usage-fleet-pause` fájlt. Ezt sem az ágens API, sem a Telegram hibája nem akadályozhatja meg. Ha a folyamat a döntés mentése és a pause-fájl létrehozása között áll le, a következő futás a tartós döntésből helyreállítja a technikai pause-t. Ez az ismételt végrehajtás stale bemenet mellett is megtörténhet, küldés nélkül.

A `level=90` akkor véglegesül, amikor mind a négy ágens POST-kérésére megvan az API elfogadási nyugta. A Telegram sikere nem feltétel. A pause-fájl és a régi level közötti rövid átmenetet a tartós `pending` esemény jelöli, ezért a két fájl közötti megszakadás helyreállítható.

**Feloldás:** csak akkor törli a pause-fájlt, amikor a négy ágens API-kérése lezárult, majd tartósan `level=0` állapotot ment. A Telegram hibája ezt nem tartja fel. Ha a törlés után, de a level mentése előtt áll le a folyamat, a már mentett ágensnyugták alapján újraküldés nélkül fejezi be a feloldást. Az owner feloldó Telegramját nem küldi el az ágensoldali lezárás előtt, így nem állít idő előtt sikeres feloldást.

**Telegram:** külön értesítési várólista, eseményenként legfeljebb három kísérlettel. Minden kísérlet sorszáma a hálózati hatás előtt tartósan mentődik. Sikertelen küldésnél a következő, megbízható adatot feldolgozó futás próbálkozik újra; a harmadik után `telegram_abandoned` hibakód naplózódik és az értesítés törlődik. Tartós tokenhiba nem fogja meg a level-váltást. Stale futás nem küld és nem fogyaszt értesítési próbálkozást. Egy új döntés a korábbi, már elavult Telegramot felülírja; a csak 80-as figyelmeztetés csendes megszűnése a hozzá tartozó régi Telegramot is törli.

A Telegram-próbaszám foglalása miatt a harmadik kísérlet közbeni processzhalál sem eredményez negyedik küldést. Az azonos értesítés már létező próbálkozási kerete migrációs jelölő hiányában sem nullázódik. A felhasznált keret megmarad akkor is, ha a harmadik hiba utáni törlő állapotírás sikertelen.

A kimenet továbbra is egy sor. A `telegram=retry(1/3)` vagy `(2/3)` jelzi a függő értesítést; `telegram=abandoned(3/3)` jelzi az eldobást. Újrapróbálandó hiba nem nulla kilépési kódot ad. A harmadik, naplózott eldobás után az értesítés már nem tartja hibás állapotban a monitort. A `--dry-run` nem küld és semmilyen fájlt sem ír, a Telegram következő lépését is csak előnézetként jelzi.

## Bemeneti és pause-olvasási szabályok

A megbízható source: `authoritative`, `authoritative_cached` vagy `authoritative_statusline`. A `generated_at` legfeljebb 25 perces és nem lehet jövőbeli. Ha `authoritative_collected_at_unix` is szerepel, arra is ugyanaz a korlát érvényes, így egy friss fájlírás nem fiatalítja meg a régi mérést. Mindkét százalék valódi, véges, 0 és 100 közötti szám kell legyen; boolean és sztring nem számít százaléknak.

Ha **bármelyik** ablak `resets_at` értéke szám és `<= now`, az egész snapshot stale: nincs küldés, a stale epizód csendesen frissül. A **hiányzó resetidő továbbra sem akadály**: a megbízható százalékhoz tartozó értesítésben `reset: ismeretlen` jelenik meg. A nem numerikus vagy nem dátummá alakítható resetre is az előző ismeretlenidő-politika érvényes. A reset epoch másodperc, a kijelzés Europe/Budapest, CET/CEST megjelöléssel.

A pause-fájl olvasása a `src/web/usage-fleet-pause.ts` `readFleetPauseState` szemantikáját követi:

* hiányzó, üres, whitespace, olvashatatlan vagy értelmezhetetlen fájl: nem paused;
* régi, whitespace nélküli tartalomra trimelve pontos `paused` szöveg: paused;
* JSON objektum, amelyben `paused` pontosan boolean true: paused;
* `false`, 1, sztring, tömb vagy más JSON: nem paused.

Ez kezeli a régi heartbeat törlés helyetti „ürítsd” műveletét is. A monitor saját, korábban véglegesített `level=90` vagy tartós pending pause döntése ettől külön belső állapot: egy hiányzó külső fájl nem feledteti el a már meghozott döntést.

István 2026-11-07 döntése alapján a Claude-küszöbök metrikánként eltérnek:

| Claude-ablak | Figyelmeztetés | Pause | Feloldáshoz szükséges |
|---|---|---|---|
| 5 órás (`five_hour`) | >=90% | >=98% | <90% |
| heti (`seven_day`) | >=80% | >=90% | <80% |

A 90%-os ötórás szint óvatos figyelmeztetés: a munka folytatható, nem rendel el pause-t. Tényleges pause után mindkét feloldási feltételnek egyszerre kell teljesülnie. Pontosan 90% ötórás vagy 80% heti érték még tartja a már elrendelt szünetet. A csak figyelmeztető fázis mindkét figyelmeztetési küszöb alá visszatérve csendesen törlődik.

Az ismételt pause-értesítés új metrika saját pause-küszöbének első átlépésekor, illetve a metrikánként utoljára jelzett maximumhoz képest legalább 5 százalékpontos növekedéskor történik. Az ötórás 98% után ugyanabban az epizódban a 100%-os felső határ miatt nincs további +5 pontos újratüzelés. A heti 90/95/100 lépcsők megmaradnak. A forrásváltás, friss fájlidő és visszaingadozás önmagában nem új értesítés.

## Állapotkompatibilitás és korábbi várólisták

A `level=0/80/90` fáziskód, nem közös százalékküszöb: nyugalom, figyelmeztetés, pause. A régi `level=90`, aktív pause-fájl vagy tartós pending pause továbbra is már elrendelt szünet. Nem minősül automatikusan warninggá, akkor sem, ha az ötórás érték csak 91%. Friss ötórás <90 ÉS heti <80 adat, valamint a feloldó ágensnyugták szükségesek a feloldáshoz. Stale adatból nincs feloldás. Ezzel átálláskor nem indulhat el idő előtt korábban leállított munka.

A `notified_90` mező neve megmarad a kompatibilitás miatt; most is a metrikánként visszaigazolt pause-jelzések maximumát jelenti. Ötórás <98 régi jelölője nem fedi el az új 98-as első átlépést; a heti >=90 jelölők változatlanok. A hiányzó/hibás jelölők és a régi level-formátum korábbi validálása megmarad.

Az új események és Telegram-outbox opcionális `policy_version=2` mezőt kapnak. Régi pending eseménynél csak a még kiküldendő szöveg frissül, a kontrollcél, a már rögzített ágensnyugták és a `telegram_queued` állapot nem vész el. A régi Telegram-üzenet szövegének átírása nem nullázza a próbakeretet. A korábban már elfogadott távoli üzeneteket nem lehet visszavonni; az új szöveg csak a hátralévő küldésekre vonatkozik. Stale futás nem migrál/küld szöveget és nem költ Telegram-próbát; a tartós pause helyreállítása viszont stale esetben is megmarad. A fájlséma régi olvasói az új opcionális mezőket figyelmen kívül hagyhatják.

## Keretnevek, ablakok és külön szabályok

Az új Claude warning/pause/reassert/resume üzenetek ékezetes magyarul nevezik meg a Claude-keretet és az 5 órás/heti ablakot; feloldásnál mindkettőt, százalékkal és resetidővel. Nincs gondolatjel, em dash vagy dupla kötőjeles elválasztó az elküldött szövegekben.

A `usage-check.py` nevezi meg a Claude-keretet. OK: ötórás <90 ÉS heti <80; FIGYELMEZTETÉS: ötórás >=90 vagy heti >=80, STOP nélkül; STOP: ötórás >=98 vagy heti >=90. Exit 2 továbbra is STOP, 0 OK/FIGYELMEZTETÉS, 3 ISMERETLEN. A korábbi két tizedes kerekítés a kijelzésre és besorolásra együtt érvényes; a flotta-monitor a nyers, validált százalékkal dönt. A checker hibás JSON-alakra vagy túl nagy számra is fix ISMERETLEN sort ad, traceback és nyers érték nélkül.

A Codex külön előfizetési keret: az 5 órás és heti checker 80/90 jelzései változatlanok, most `Codex 5 órás keret` és `Codex heti keret` megnevezéssel. A Codex-collector nem pause-állapotgép, csak a snapshotot és fix hibaszövegeket kezeli.

A Gemini checker csak heti keretet ismer, az eddigi 80/90 jelzésekkel. Nincs 5 órás Gemini-ablak, nincs új kitalált küszöb vagy flotta-pause. A szolgáltató Claude/GPT csoportja `Gemini szolgáltatói Claude/GPT heti keret` néven jelenik meg: ez nem a Claude-előfizetés közös ötórás/heti kvótája. A gyűjtők százalékos/pause-logikája nem változott.

A `src/web/usage-fleet-pause.ts` és a router állapotfájl-fogyasztó, nincs bennük százalékos döntés; a kérés szerint érintetlenek. A router korábbi, belső usage-pause-held diagnosztikáját sem írtam át. A külön `src/quota-gate.ts` háttérmunka-takarékossági szabálya 85% nyomásnál, illetve reset előtt 70%-nál halaszt: ez nem a flotta-pause, és külön döntés híján változatlan. Ezért a háttér-heartbeat továbbra is elhalasztható anélkül, hogy a flotta pause-ba kerülne. Az owner-facing munkát a gate eddig is engedte.

A `usage-collect.py` külön pace-alapú diagnosztikai rendszere (köztük 92%-os near-exhaustion jelzés) szintén nem a flotta-állapotgép; ehhez nem nyúltam. A gyűjtő- és TS-regressziók futnak, de egyikük futtatása sem jelenti az éles gyűjtő elindítását.

## Mit jelent az ágens API nyugtája?

A jelenlegi, hitelesített, érvényes `POST /api/messages` **200-as választ és pending-be tett üzenetazonosítót ad**, a cél session pillanatnyi állapotától függetlenül. Ez a sorba állítás nyugtája, nem tényleges ágensfogyasztási vagy tmux-kézbesítési visszaigazolás.

A router később próbál kézbesíteni. A hiányzó cél sessionhez tartozó üzenetet az egyórás várakozási ablak után eldobhatja; a létező, de elfoglalt session üzenetét ez az egyórás szabály nem dobja el. Ezt a monitor nem tudja a POST válaszából megállapítani.

Az `inactive_reply` kompatibilitási ág és a mockolt `inactive` teszteset csak védekező viselkedés egy esetleges másik/jövőbeli API-válaszhoz. **Az éles API nem ad „nem futó címzett” választ, így a monitor nem állíthatja, hogy ilyen címzettet ténylegesen kihagyott.** A tesztnevek és kommentek ezt kifejezetten jelzik. A négy POST elfogadása után a monitor véglegesít; nem figyeli a router későbbi kézbesítési státuszát.

## Korábbi második kritikus ellenőrzés és javítások

1. **Telegram által fogva tartott pause/feloldás:** a fájlkezelés és a level-váltás az ágens API-nyugtákhoz igazodik, a Telegram külön várólistába került. Külön teszt a pause és a resume alatti tartós Telegram-hiba.
2. **Döntés és pause-fájl közötti leállás:** a tartós pending döntésből a következő futás visszaépíti a fájlt, stale bemenetnél is, minden hálózati küldés nélkül.
3. **Idő előtt elküldött feloldó owner-üzenet:** félbemaradt ágensoldali feloldásnál a Telegram kísérlete halasztódik, a próbakeret fogyasztása nélkül.
4. **Lejárt reset:** bármelyik ablak számszerű, múltbeli vagy pontosan mostani resetje csendes stale; hiányzó reset továbbra is megengedett. Az inkluzív határ, negatív és szélsőséges értékek is teszteltek.
5. **Üres pause-fájl miatti hamis feloldás:** a fájl puszta létezése helyett a routerrel egyező értelmezés működik. Üres és hibás fájl önmagában nem indít feloldó üzenetet.
6. **Próbakeret újraindulása migrációkor:** a stresszteszt kimutatta, hogy hiányzó migrációs jelölő mellett az azonos, már létező Telegram-várólista 2-es próbakerete 0-ra állhatott. Javítva: azonos kind és szöveg esetén a meglévő keret megmarad. A harmadik kísérlet után eldobódik.
7. **Elavult Telegram újrajátszása:** új pause/resume felülírja a régi értesítést, a megszűnt 80-as figyelmeztetés törli saját várólistáját. Egy Telegram-hiba után már lezárt feloldásra érkező 85% új figyelmeztetés, nem új hard pause.
8. **Megszakítás minden tartós írási határon:** kilenc pause és nyolc resume írási ponton szimulált leállás után az újrafutás a helyes levelhez és pause-fájlhoz jutott, ágensüzenet-ismétlés nélkül. Külön eset a pause törlése utáni leállás és a harmadik Telegram-kísérlet megszakítása.
9. **Íráshibák:** a kezdeti pending/pause íráshiba előtt nincs küldés; a Telegram próbafoglalás íráshibája előtt nincs Telegram-hívás. A harmadik hiba utáni törlés meghiúsulása nem ad új próbakeretet.
10. **Korábbi védelmek regressziója:** párhuzamos valódi folyamatok, 1000 kétmetrikás ingadozás, 79.99/80.0/90.0 határok, DST/epoch, számtípusok, symlink, tokenredakció, dry run és részleges API-elfogadás tesztjei továbbra is futnak. A napló csak fix hibakódot tartalmaz; nincs tokenes URL, HTTP törzs vagy nyers kivétel.

## Korábbi tesztkimenet

A teljes frissített monitorcsomag:

```text
python3 -B scripts/__tests__/usage-fleet-monitor.test.py
Ran 75 tests in 1.110s
OK
```

A megszakítási mátrix 17 külön checkpoint-esetet tartalmaz a teszten belül, a 75 tesztmódszeren felül. A tesztek ideiglenes fájlokkal és mockolt küldéssel futnak; nincs éles API-kérés.

A kapcsolódó meglévő tesztcsomagok eredménye:

```text
usage-check.test.py: Ran 4 tests in 0.555s, OK
usage-monitor-shortcircuit.test.py: Ran 2 tests in 0.001s, OK
usage-collect.test.py izolált LATEST_PATH mellett: Ran 123 tests in 2.549s, OK
```

A monitorral együtt összesen **204 sikeres tesztmódszer**. A collector egyik meglévő tesztje nem izolálja a valódi `LATEST_PATH` cache-t; a teljes collectorcsomag ezért ideiglenes, hiányzó `LATEST_PATH` beállítással futott. A collector kódja és tesztje nem változott.

## Telepítési lépések, csak későbbi operátori végrehajtásra

Ezek **utasítások a majdani telepítéshez, nem végrehajtott műveletek**.

1. **Először tiltsd le a régi `usage-monitor` scheduled taskot.** A dashboardban állítsd disabled állapotba, vagy az operátori konfigurációkezelés szerint a `~/.claude/scheduled-tasks/usage-monitor/task-config.json` `enabled` értékét állítsd false-ra. Ellenőrizd, hogy nincs futó régi usage-monitor forduló. A modellalapú és az új monitor ne fusson párhuzamosan.
2. Másold át/alkalmazd az ellenőrzött fájlokat a kívánt repóba. A service sablon **mindkét** `/path/to/marveen` helykitöltőjét cseréld a tényleges abszolút repoútra, például `/home/kisss/marveen`. A timer a `usage-fleet-monitor.service` nevet használja.
3. WSL-ben ellenőrizd, hogy a disztribúció systemd támogatással indul és működik a user systemd manager. Az operátor engedélyezze a felhasználó lingeringjét, például `sudo loginctl enable-linger "$(id -un)"`; ezt jogosultsági ellenőrzés után maga az operátor futtassa. A linger a user manager kijelentkezés utáni működését segíti, de önmagában nem indítja el és nem tartja életben a WSL virtuális gépet Windows alatt. A distro életciklusa külön üzemeltetési feladat.
4. A kitöltött két unitot másold a `~/.config/systemd/user/` könyvtárba. Ellenőrizd őket: `systemd-analyze --user verify ~/.config/systemd/user/usage-fleet-monitor.service ~/.config/systemd/user/usage-fleet-monitor.timer`.
5. Futtasd a `systemctl --user daemon-reload` parancsot, majd a monitor `--dry-run` módját a célrepóból. Ellenőrizd a `store/usage-latest.json`, `.dashboard-token` és a repo `.env` jogosultságait és elérhetőségét. A tokeneket ne másold parancssorba vagy a journalba.
6. Csak az előző ellenőrzések és az operátori átállítási döntés után: `systemctl --user enable --now usage-fleet-monitor.timer`. Ezután ellenőrizd a `systemctl --user list-timers usage-fleet-monitor.timer` és a `journalctl --user -u usage-fleet-monitor.service` kimenetét.
7. Ellenőrizd újra, hogy a régi scheduled task disabled. Visszaállításkor előbb az új timert állítsd le és tiltsd le, és csak utána aktiváld a régi monitort.

Az `After=network-online.target` sort eltávolítottam a user-service-ből. A user manager nem a system manager hálózati targetjét követi, ezért ez nem adott volna valós hálózati készenléti garanciát. A szkript időkorlátos hívásokkal és tartós újrapróbálással kezeli a hálózati hibát. A félórás timer továbbra is `Persistent=true` értékű, de csak futó systemd/WSL mellett működik.

## Ellenőrzési korlátok

* Nem történt éles dashboard vagy Telegram küldés, valódi tokenjogosultság- vagy router-kézbesítési próba. A POST nyugta és a későbbi tényleges kézbesítés eltérése ismert korlát.
* A több fájl és a távoli API között nincs közös atomikus tranzakció. A tartós pending és a nyugták a tesztelt írási határokon helyreállítanak; a távoli elfogadás és a helyi nyugta mentése közötti processzhalál továbbra is megismételhet egy üzenetet. A Telegram foglalás ekkor is legfeljebb három kísérletre korlátoz.
* A systemd unitok éles szintaktikai és WSL futáspróbája nem történt meg ebben a sandboxban. A telepítési parancsok nem futottak le.
* Nem történt commit, push, dashboard-újraindítás, timertelepítés vagy aktiválás, illetve régi scheduled task módosítás. A router védett ágenslistája és a 2/6 feladat fájljai érintetlenek maradtak.

## 2026-11-07 átadás és aktiválás

Az új saját stresszellenőrzés eredményei, pontos parancsai és a megmaradó korlátok a munkamásolat `self-review.md` és `test-results.txt` fájljában találhatók. Az eredeti és módosított fájl:sor leltár: `inventory.md`. A scheduled task pontos javasolt teljes csereszövege: `docs/usage-monitor-SKILL.replacement.md`; hozzá külön `scheduled-task.patch` készült. Az éles ~/.claude fájl nem módosult.

A Python-fájlokhoz nincs build vagy szolgáltatásrestart: az ellenőrzött új kód a következő futáskor töltődik be. TS-kód nem változott, ezért ahhoz sincs build/restart-igény. A már futó Claude-session beolvasott CLAUDE.md utasítása nem frissül magától; az új szabályt a következő sessionbetöltésnél vagy operátori szabályfrissítéssel kell érvényesíteni. Scheduled task és systemd timer közül egyszerre egy aktív usage-vezérlő maradjon. E munkában sem build, sem restart, sem aktiválás nem történt.
