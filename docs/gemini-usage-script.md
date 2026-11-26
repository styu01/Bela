# Gemini usage mérés modellhívás nélkül

A gyűjtő az Antigravity/agy két heti kvótacsoportját olvassa: Gemini Models és Claude and GPT models. A szolgáltatói parancs kizárólag `/usage`. A saját Python-kód nem olvas auth-, token- vagy beszélgetésfájlt és nem küld HTTP-kérést. Az agy a már bejelentkezett provider-home környezetben indul; a szolgáltatói program saját bejelentkezési működését nem változtatja meg.

## Futás

A repo gyökeréből, későbbi operátori használatra:

```sh
/usr/bin/python3 scripts/gemini-usage-collect.py
/usr/bin/python3 scripts/gemini-usage-check.py
```

A gyűjtő gyermekfolyamatának pontos argumentumai:

```text
/home/kisss/.local/bin/agy --print /usage --print-timeout 30s
```

Csak ennek a gyermeknek a HOME értéke `/home/kisss/.local/share/marveen-gemini-bridge/provider-home`. A szülő környezete változatlan. A gyermek csak PATH, HOME, LANG és TZ változót kap; az XDG_*, GEMINI_*, GOOGLE_* és minden más változó nem öröklődik. Nincs shellfuttatás vagy prompt-helyettesítés. Külső időkorlát 45 másodperc; siker, hiba vagy időtúllépés után is killpg hívással megszakítja a folyamatcsoportot és megvárja a közvetlen gyermeket. A már megszűnt folyamatcsoport ProcessLookupError hibája elfogadott. A stdout és stderr együttesen legfeljebb 8192 bájt; a stderr tartalmát nem naplózza.

**Tilos `/status`-ra vagy sima promptra cserélni a parancsot: ezek modellfordulót és kvótafogyasztást indíthatnak.** A bejelentkezési fájlokat ne nyissátok meg a méréshez. A `--agy` kapcsoló TETSZŐLEGES binárist futtat a megadott útvonalról, ezért kizárólag ellenőrzött tesztprogrammal, teszteléshez használd. Nem ellenőrzi a bináris kilétét vagy modellmentességét. Az argumentumok ettől függetlenül mindig a rögzített --print /usage --print-timeout 30s értékek; normál használatban maradjon az alapértelmezett, megadott agy.

A TSV pontosan két sort és soronként négy, egyetlen tabbal elválasztott mezőt tartalmazhat. Csak a két ismert csoport és a `Weekly Limit Remaining` címke engedett, csoportonként egyszer. A sorrend felcserélhető, CRLF és egyetlen opcionális záró LF megengedett. Nem splitlines() alapján bont: csak CRLF-et alakít LF-re, majd LF mentén darabol. A form feed, vertical tab, 0x1c/0x1d/0x1e, 0x85, U+2028 és U+2029 érvénytelen, nem válhat észrevétlen sorelválasztóvá. Hiányzó, extra vagy üres sor, duplázott tab és idegen szöveg hiba. A százalék 0 és 100 között lehet, legfeljebb hat tizedesjeggyel, a reset pedig érvényes, időzónás ISO dátum/idő. A resetet UTC-re egységesíti. A múltbeli reset önmagában nem gyűjtői parserhiba, de az ellenőrző friss adaton is ISMERETLEN (reset) sort ad, ha a gemini csoport resets_at értéke kisebb vagy egyenlő az aktuális időnél. A claude_gpt csoport lejárt resetje nem teszi ISMERETLEN-né a Gemini sort, csak a saját adatainál jelenik meg a reset elmúlt megjegyzés.

## Pillanatkép és hibák

A `store/gemini-usage-latest.json` alakja:

```json
{
  "collectedAt": "2026-11-06T06:00:00Z",
  "groups": {
    "gemini": {"remaining_pct": 83, "used_pct": 17, "resets_at": "2026-11-10T10:00:49Z"},
    "claude_gpt": {"remaining_pct": 100, "used_pct": 0, "resets_at": "2026-11-13T09:45:34Z"}
  }
}
```

Ez szintetikus példa a megadott mintából, nem éles mérési eredmény. A used_pct = 100 - remaining_pct. A teljes adat érvényesítése után privát tmp-fájlt ír, fsync után atomikusan cseréli a pillanatképet és fsynceli a könyvtárat. Hibás/üres/időtúllépett vagy nem nulla státuszú szolgáltatói válasz előtt nem ír pillanatképet, tehát a korábbi jó adat megmarad. Rename előtti íráshibánál is megmarad a régi fájl. A cserét követő könyvtár-fsync hibája jelezhet hibát már új, de érvényes pillanatkép mellett.

A `store/.gemini-usage-collect.lock` egyidejű gyűjtést kizár. A state-könyvtár létrehozása és megnyitása sem követ symlinkelt szülőt. A lock és a log csak normál fájl lehet, symlink és FIFO nem használható helyettük. A snapshot és az új log/lock 0600, az új könyvtár 0700 jogú. Foglalt locknál exit 75, egyéb hibánál exit 1; siker exit 0.

A `store/gemini-usage-collect.log` csak UTC időt és fix hibakódot kap: provider_unavailable, provider_failed, provider_timeout, provider_output_too_large, invalid_output, snapshot_write_failed, store_failed, lock_failed, busy vagy invalid_now. A state-könyvtár előkészítésének open_store/mkdir hibája store_failed, a lockfájl hibája külön lock_failed. A szolgáltatói stdout/stderr, kivételszöveg, környezet és titok nem kerül bele. Ha a log sem írható, fix log_write_failed kerül stderr-re.

## Ellenőrző

A check csak a pillanatképet olvassa, agy-t vagy modellt soha nem indít. Egyetlen sort ír, a Codex-check stílusában:

```text
Gemini heti keret 17% OK, maradt: 83%, reset: 2026-11-10T10:00:49Z | Gemini szolgáltatói Claude/GPT heti keret 0% használt, maradt: 100%, reset: 2026-11-13T09:45:34Z
```

Érvényes, friss és még vissza nem állt kvótánál a státuszt csak a gemini.used_pct határozza meg: 80 alatt OK, 80-tól FIGYELMEZTETÉS, 90-től STOP. Minden százalék legfeljebb két tizedesre, ROUND_HALF_UP szabállyal kerekítve jelenik meg, felesleges nullák és tudományos jelölés nélkül. A státusz pontosan a kijelzett, kerekített used_pct értékből számolódik: például 79,999 esetén 80% FIGYELMEZTETÉS, nem 80% OK. A két tizedes ROUND_HALF_UP szándékosan konzervatív: 89.995 már 90% STOP-ra kerekül, tehát a kijelzett határértékhez tartozó szigorúbb státusz érvényesül. A Claude/GPT csoport tájékoztató, még a saját 90 feletti használata sem állítja STOP-ra a Gemini sort. Hiányzó/sérült vagy inkonzisztens adat ISMERETLEN. Pontosan 90 perces adat még friss, ennél régebbi vagy jövőbeli collectedAt ISMERETLEN (stale). Hiba esetén sem ír ki idegen fájltartalmat. A checker kilépési kódja a Codex-checkhez hasonlóan 0, az állapotot a sora közli.

A gyűjtő `--state-dir`, `--agy`, `--now` kapcsolóval, a checker `--input`, `--now` kapcsolóval tesztelhető. A --now időzónás ISO idő. A gyűjtőben kizárólag a collectedAt tesztértékét állítja: sem a szolgáltatói logtakarítás határidejét, sem a hibalog valós idejét nem befolyásolja. A checker --now kapcsolója külön, az ellenőrzési idő tesztelésére szolgál. A kapcsolók nem teszik változtathatóvá a `/usage` parancsot.

## Időzítő és illesztés

A `scripts/systemd/gemini-usage-collect.service` és `.timer` csak sablon. `/path/to/marveen` helyőrzővel, abszolút /usr/bin/python3 indítással, félóránkénti OnCalendar és Persistent=true beállítással készültek. A service TimeoutStartSec=60 és SuccessExitStatus=75, tehát a foglalt gyűjtői lock nem jelöl failed service-futást. A félórás gyűjtéshez a 90 perces frissességi keret kimaradt futás esetére tartalékot ad. Nincs telepítés, engedélyezés, dashboard-újraindítás, Telegram vagy inter-agent értesítés.

A usage-fleet-monitor.py továbbra is kizárólag a Claude-előfizetés pause-át kezeli; a 2026-11-07-i Claude-küszöbmódosítás a Gemini 80/90 jelzéseit nem változtatja meg. Jelenlegi snapshot() függvénye Claude-forrást és five_hour/seven_day ablakokat vár. A későbbi Gemini-illesztéshez külön beolvasó/normalizáló adapter és szolgáltatónként külön állapot szükséges; a Gemini heti csoportját nem szabad ötórás ablaknak álcázni. A közös feloldásnál külön szabály kell ahhoz, hogy egy Claude-reset ne oldhasson fel egy Gemini STOP állapotot, és az elavult adat ne jelentsen hamis megnyugvást.

## Megosztott provider-home és logtakarítás

A gyűjtő ugyanazt a provider-home állapotot használja, mint a Gemini bridge. Bridge-feladat közben is futhat: a gyűjtő saját flockja csak egy másik gyűjtőt zár ki, a bridge-feladattal nem közös lock. Az agy kizárólag /usage parancsot kap, a Python-kód auth-/tokenfájlt nem olvas és a bridge állapotát nem próbálja átírni.

A szolgáltatói logok takarítása kizárólag az alapértelmezett /home/kisss/.local/bin/agy útvonallal futó gyűjtő sikeres és hibás kísérletének végén történik, a gyűjtő lockján belül. Egyedi vagy fake --agy mellett nincs takarítás, az egyedi --state-dir sem tereli át a szolgáltatói logok helyét. Az alapértelmezett binárisnál az egyedi --state-dir önmagában nem kapcsolja ki a takarítást. Az időt a takarítás saját datetime.now(timezone.utc) hívásából veszi, nem a collectedAt vagy a --now értékéből. Kizárólag a /home/kisss/.local/share/marveen-gemini-bridge/provider-home/.gemini/antigravity-cli/log mappa normál cli-*.log fájljai érintettek. A szűrés fix prefix és .log végződés; nincs rekurzió, nincs tetszőleges takarítási útvonal-kapcsoló. A gyűjtő itt a bridge SAJÁT agy cli-*.log fájljait is törli, nem csak a saját futásai naplóit. Diagnosztikai értékük miatt a retenció 30 nap. Csak a módosítási idő szerint 30 napnál szigorúan régebbi fájl törlődik. A pontosan 30 napos, friss, más nevű, könyvtár vagy symlink bejegyzés megmarad. A mappa őseinél sincs symlinkkövetés. Tartalmat nem olvas; minden fájlrendszeri takarítási hiba csendben figyelmen kívül marad, nem ront el egy sikeres mérést. Ez nem a saját gemini-usage-collect.log rotációja, hanem a megadott szolgáltatói logok takarítása.

## Ismert korlátok

A korábbi TOCTOU-korlát megmarad: a pillanatkép tmp/replace szakasza a korábban ellenőrzött könyvtár útvonalát újra használja. Egy azonos jogosultságú folyamat közbeni átnevezése/útvonalcseréje ellen ez nem teljes fájlrendszeri izoláció. A logtakarítás stat és unlink művelete között is változhat a bejegyzés, különösen mert a bridge ugyanazt a területet használja; symlink célját ettől sem követi, de a névhez tartozó fájl lecserélődhet. E körben ezeket nem alakítottuk át teljes tranzakcióvá.

A collectedAt továbbra is a gyűjtési kísérlet elején rögzített idő, nem a szolgáltató saját megfigyelési időbélyege és nem a fájlcsere pillanata. Emiatt a lekérdezés idejével konzervatívan korábbi lehet. A --now ezt teszteléshez felülírhatja; normál éles futásnál ne használd.

## Ellenőrzés

```sh
/usr/bin/python3 -B scripts/__tests__/gemini-usage-collect.test.py
```

47 automata teszt zöld. A tesztek kizárólag ideiglenes fake agy-t, ideiglenes provider-home/log-fixture-t, rövid időkorlátot és szintetikus snapshotokat használnak. A valódi provider-home takarítása a tesztekben nem fut. Tesztelt a négyelemű környezeti allowlist, sikeres ágban is megszakított fake unoka, kerekítési határ, lejárt reset, tiltott sorelválasztók, store_failed és a szigorúan korlátozott 30 napos takarítás. Külön próba ellenőrzi, hogy --now 2030 nem törölhet valós idő szerint 29 napos logot, fake --agy mellett nincs takarítás, és a lejárt Claude/GPT reset csak tájékoztató. Lefedik a parserhibákat, 80/90 és 90 perces határt, hibás második csoportot, fix argumentumokat és gyermek-HOME-ot, timeoutot és részleges stdoutot, nagy/hibás kódolású kimenetet, atomikus cserét, hibás írást, foglalt lockot, FIFO/symlink-védelmet és titokmentes naplózást. A valódi agy, a valódi szolgáltatói bejelentkezés és az éles systemd-futás nincs kipróbálva ebben az implementációs körben.
