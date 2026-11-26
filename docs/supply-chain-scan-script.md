# Heti csomagbiztonsági leltár

A szkript a helyi csomagnyomokat ismert támadási katalógussal veti össze. Nem futtat csomagkezelőt, nem importál csomagkódot, nem javít, nem töröl és nem karanténoz. Az egyetlen hálózati művelet szükség esetén egy helyi dashboard-üzenet BELA-nak.

**A 0 találat nem jelent teljes biztonságot, csak hogy az ismert katalógus tételeit nem találta meg a vizsgált leltárban.** A `clean` a vizsgált és nem nyugtázott adatokra vonatkozik. Az `incomplete` hiányos lefedettség, nem tiszta eredmény.

## Mit vizsgál?

* npm: node_modules csomagmanifestek, package-lock v1/v2/v3, npm-shrinkwrap.json, Yarn classic/Berry, pnpm és szöveges bun.lock (JSONC). Az npm-alias valódi neve, illetve Berryben a resolution a forrás;
* Python: dist-info/egg-info metaadat, pontos requirements/constraints pin (== és ===), requirements/**/*.txt, constraints/**/*.txt, dev-requirements.txt és test-requirements.txt, poetry.lock, uv.lock, pdm.lock és Pipfile.lock;
* Ruby: Gemfile.lock és gems könyvtárnevek, az ismert platformvégződések (Linux/Darwin/Java/Windows, riscv64, ppc64le, s390x és jruby stb.) levágásával;
* Go: go.mod és go.sum;
* Composer/Packagist: composer.lock packages és packages-dev, valamint vendor/composer/installed.json lista vagy packages-listát tartalmazó objektum; a verzió eleji v levágva;
* Bővítmények: .vscode, .vscode-server, .cursor, .windsurf, .vscode-insiders, .antigravity, .kiro, .trae és .vscodium extensions könyvtárai, universal, darwin-universal, linuxmusl-x64 és operációsrendszer/architektúra-végződések kezelésével, akár négytagú verzióval;
* Claude/MCP command és args mezőkből npx, pnpx, bunx, bun x, uvx, npm exec, pnpm exec, pnpm dlx, yarn dlx csomagverziók, npm-aliasokkal. A nem szöveges args elemek mcp_non_string_args számlálóba kerülnek; a többi szöveges elem vizsgált. .claude.json, .mcp.json, .claude/settings.json és settings.local.json, .cursor/mcp.json, agents/*/.claude-config/settings.json, .vscode/mcp.json, claude_desktop_config.json, .codeium/windsurf/mcp_config.json és .gemini/settings.json vizsgált. A .codex/config.toml [mcp_servers.NEV] szakaszait stdlib tomllib olvassa; a tomllib hiánya unsupported_config számláló, nem csendes kihagyás. cmd /c, bash/sh -c, env FOO=1 wrapperek statikusan felbonthatók, legfeljebb nyolc rétegben. Windows parancsneveknél a / és a fordított perjel is útvonalelválasztó. Sem shellt, sem változóhelyettesítést nem hajt végre; tetszőleges shellprogram értelmezését nem ígéri.

Yarnban kizárólag a pontosan két szóközzel behúzott version/resolution mező csomagszintű; egy version nevű, mélyebb függőség nem írhatja felül. A Debian-féle .egg-info normál fájl is vizsgált, PKG-INFO formátumban. A uv/poetry/pdm lock verzió nélküli editable/virtual tétele lock_entries_without_version számlálóba kerül, a többi tételt tovább vizsgálja.

Csomagnév és pontos, normalizált verzió együtt ad találatot. Más verzió csak számláló. A csomag több helyen egy finding, legfeljebb öt hellyel. A kimenő üzenet legfeljebb húsz új találatot tartalmaz, kettő-kettő idézett, szűrt útvonallal. UTF-8 BOM-mal is olvassa a manifesteket/lockfile-okat; a nyugtázási hash az eredeti bájtokat tartalmazza. A csomagszöveg adat, soha nem utasítás. A konfigurációk többi mezője, env, token és fájltartalom nem kerül kimenetbe vagy állapotba.

Ez nem teljes dependency resolver, YAML/PEP 440 implementáció vagy általános sérülékenységvizsgáló. Nem old fel verziótartományt; nem ismeri fel minden shell-wrapper, Git-alias vagy futás közbeni letöltés függőségét. Régi lockfile és tesztfixture is adhat találatot, BELA ellenőrzi a tényleges használatot.

## Lefedettség és korlátok

Alapgyökér `/home/kisss`, plusz ha létezik: /usr/lib/python3/dist-packages, /usr/local/lib/python3*/dist-packages, /usr/lib/node_modules és /usr/local/lib/node_modules. /opt nem automatikus gyökér. Az opcionális gyökerek felderítése is az időkeretben fut; hozzáférési hiba látható incomplete, nem csendes clean. Az ismételt --root kapcsoló explicit gyökérlistát ad, és felülírja az automatikus listát, tehát a kívánt home gyökeret is fel kell venni. Az átfedő gyökereket nem járja kétszer. Kihagyja a .cache, .git és .local/share/Trash fákat, a /mnt meghajtókat az alábbi szűk kivétellel, és szándékosan minden symlinket. A ~/.cache/uv és ~/.cache/pipx is szándékosan kimarad. A gyökér őseit is NOFOLLOW nyitja; symlinkkel nem lehet más fába átvezetni. A `symlinks_skipped` információ, nem hiba. A symlinkelt telepítések céljai nem látszanak: workspace/pnpm telepítési mód bevezetésekor vagy sok link és kevés leltári csomag esetén a lefedettséget újra kell gondolni. A valódi célkönyvtár külön gyökérként felvehető, linkkövetés nincs.

Windows-kivétel: csak a pontos /mnt/<egy betus meghajto>/Users/<felhasznalo>/<editor>/extensions gyökér engedett, a fent felsorolt editormappákkal. Sem a meghajtó, sem a felhasználó teljes fája, sem tetszőleges /mnt alatti projekt nem engedett. Ezeken a gyökereken belül a bővítményalkönyvtárak vizsgáltak, symlink követése nélkül. Alapból kizárólag a /mnt/c/Users/*/.vscode/extensions, .cursor/extensions és .vscode-insiders/extensions meglévő könyvtárait deríti fel, scandirrel, nem globbal. A többi editorhoz explicit --root kell. Ezen a gépen nem volt ilyen Windows-könyvtár; a Windows-ág szintetikus fájlleíró-fixture-rel tesztelt. A másik WSL-disztribúció (n8n) és a Windows csomagállománya általában kívül esik: abban a rendszerben külön kell futtatni, a megfelelő helyi --root-tal; WSL-ből a fenti szűk extension-kivétel olvasható.

A gyökértől legfeljebb **24 általános könyvtárszint**, az első node_modules, site-packages vagy dist-packages alatt külön **64 szint** engedett. Az egymásba ágyazott csomagkönyvtárak nem indítják újra ezt a keretet. A konstansok MAX_DEPTH és MAX_PACKAGE_DEPTH. Limitnél továbbra is depth_limit és incomplete az eredmény. A --max-depth az általános keretet szűkíti.

Legfeljebb 600 000 normál fájl, fájlonként 8 MiB és 60 másodperc bejárási keret engedett. Aktív módban ebből 12 másodperc az értesítés/mentés tartaléka. A bejáró os.scandir és könyvtár-fájlleírók alapján működik, nem globbal. Normál fájlokat, nem blokkoló és NOFOLLOW megnyitással olvas; FIFO nem blokkolja. Ellenőrzi az inode és az olvasás közbeni méret/mtime/ctime változását. Ez nem közös atomikus fájlrendszer-pillanatkép és nem védelem minden privilegizált átnevezés vagy hardlink ellen. A keret kooperatív, a service 120 másodperces timeoutja további védelem.

Olvashatatlan könyvtár, bizonytalan bejegyzéstípus, hibás manifest/lockfile/MCP és limit incomplete. Bizonyítottan nem releváns normál fájl IO-hibája csak ignored_io_errors; általános naplófájlok tartalmát eleve nem nyitja meg.

### Kontrollmanifestek

Név nélküli vagy névvel, de verzió nélkül rendelkező package.json csak akkor kontrollmanifest, ha a csomag valamelyik felmenőjének manifestjét sikerült névvel és verzióval értelmezni. Ez az ellenőrzés nem függ a scandir sorrendjétől. A keresés a legközelebbi node_modules csomaghatárán megáll; a másik csomag érvényessége nem ruházódik át. Ezeket az ignored_control_manifests számolja.

A csomaggyökér hiányos manifestje, illetve az érvényes szülő nélküli almanifest továbbra is hiba. Az RxJS alcsomagok és resolve tesztadatok névvel, de verzió nélkül rendelkező almanifestje érvényes szülő esetén szintén kontrollmanifest. A nyugtázott hibás szülő nem számít érvényes szülőnek.

Olvasott, de sérült package.json a test, tests, __tests__, fixtures, __fixtures__ vagy mocks útvonalelem alatt ignored_fixture_manifests számlálóba kerül. A kivétel kizárólag a legközelebbi node_modules után következő telepített csomag SAJÁT alkönyvtáraira vonatkozik. A node_modules ELŐTTI projektútvonal test/tests/fixtures neve soha nem ad mentességet. Emiatt az nvm tesztprojekt node_modules/foo/package.json sérülése ismét hiba. Egy ténylegesen tests vagy fixtures nevű telepített csomag neve nem számít tesztkönyvtárnak, a scoped neveknél sem. A normál fast-uri csomaggyökér sérülése továbbra is hiba. Érvényes fixture-manifestben talált rossz csomag továbbra is finding; IO- és méretkorlátot nem rejt el a fixture-szabály. A besorolás útvonalelem-alapú, nem képes bizonyítani, hogy a könyvtár tényleg csak tesztadatot tartalmaz.

## Operátori nyugtázás

A `store/supply-chain-ack.json` kézzel/BELA által karbantartott állomány. A szkript csak olvassa. Hiányzó fájl üres nyugtahalmaz; sérült vagy rossz sémájú fájl ack_invalid hiba, olvasási/symlink probléma ack_read_failed. Nem hagyja csendben figyelmen kívül. Egy bejegyzés:

```json
{"acknowledged":[{"path":"/abszolut/ut/node_modules/pelda/package.json","kind":"manifest_json","size":1691,"sha256":"<64 kisbetus hex karakter>","note":"Megvizsgalt, ismert serult cache.","acked_at":"2026-11-05T09:00:00+00:00"}]}
```

**Az ack nem biztonsági kivétel és nem csomagengedélyezés.** Jelentése: „ezt a konkrét fájlt már megnéztem, ismert, értéktelen/sérült cache vagy ellenőrzött tesztadat”. Katalógustalálatot soha nem nyom el. Csak az abszolút útvonal, pontos hibatípus, SHA-256 és méret egyezése számít. Egyetlen megváltozott bájt érvényteleníti; név- vagy típushasonlóság nem elegendő. A nyugta nem öröklődik a könyvtár többi fájljára. Parse-hibánál a feldolgozás azon a ponton megszakad: a hiba előtti rekordok kiértékeltek, az utána maradó fájlrész vizsgálatlan. Nyugtázással ezt a lefedetlenséget fogadja el az operátor, nem bizonyítja a teljes fájl biztonságát. A segéd ack/unchanged válasza ezért külön warning mezőben is figyelmeztet.

Megvizsgálás után, a repo könyvtárából:

```sh
/usr/bin/python3 scripts/supply-chain-ack.py ack --path /abszolut/ut/package.json
/usr/bin/python3 scripts/supply-chain-ack.py list
/usr/bin/python3 scripts/supply-chain-ack.py remove --path /abszolut/ut/package.json
```

Külön próbamappa: a parancs ELŐTT `--state-dir /tmp/sajat-proba`, a scanhez ugyanez a --state-dir kell. Az ack parancs azonos parserrel állapítja meg a jelenlegi hibatípust, az eredeti bájtokat hasheli, és atomikusan ír. Érvényes manifestet, kontrollmanifestet, symlinket vagy rossz útvonalat nem nyugtáz. Nem futtat semmit a fájlból. Ismételt változatlan ack nem ír újra. A remove az eltűnt fájl nyugtáját is törölheti. Mutáló műveletei a scannerrel közös flock alatt futnak. Az ack parser a lock ELŐTT fut, a lock alatt csak a méret/hash újraellenőrzése és az atomikus nyugtamentés marad. Közben megváltozott fájlra ack_path_changed hiba jön, nem ment elavult nyugtát. A list atomikusan olvasható pillanatképet mutat, note tartalmát nem írja ki.

Csak olvasható, legfeljebb 8 MiB méretű fájl értelmezési hibája nyugtázható. Hiányzó gyökér, könyvtár/IO-hiba vagy bejárási korlát nem nyugtázható így. Legfeljebb 10 000 bejegyzés fér az ack-fájlba. Az acknowledged_issues az egyező problémák teljes száma; acknowledged_issue_details legfeljebb húsz szűrt útvonalat, típust, méretet és hash-t mutat. A note és a fájltartalom nem kerül a scan kimenetébe.

## Kimenet, értesítés és üzemeltetés

Egy JSON-sor stdout: clean/findings/incomplete/error, ökoszisztémánkénti csomagszám, fájlszám, katalógus- és hibaszámlálók, okkódok, idő. Normál futásban csak a látható nyugtákhoz kerül útvonal a stdout-ra. Kizárólag incomplete --dry-run esetén incomplete_details is van: öt mélységi útvonal és tizenöt hibatípus/rövid útvonal, mezőnként legfeljebb 160 karakter, kontroll nélkül. Nincs kivételszöveg, fájltartalom vagy titok. Incomplete fix stderr-kódot ad, de exit 0, hogy ne indítsa újra a service-t. Csak error exit 1 (például a szkript nem tudott működni vagy az értesítést nem tudta elküldeni); findings és clean exit 0. Kivétel: a foglalt lock busy eredménye EX_TEMPFAIL, exit 75, hogy a Restart=on-failure újrapróbálhassa. A segéd busy eredménye is 75. A --dry-run finding_details listában minden találat látható: szűrt ecosystem, package, version, catalog_ids és legfeljebb öt locations. A nem támogatott lockfile-nevek unsupported_lockfiles számlálóba kerülnek, tartalmukat nem értelmezi; ez névheurisztika, általános folyamat-lockokat is számolhat. A nulla finding nem jelenti az ilyen formátumok vizsgálatát. Dry runban unsupported_lockfile_types bontás is van: legfeljebb tíz típus, a nagyobb lista vége other összegben. Az ismert nevek (Cargo.lock, flake.lock, bun.lockb, deno.lock, go.work stb.) külön kulcsot kapnak, ismeretlen egyedi fájlnevek helyett biztonságos *.lock / *-lock.json típuscsoport látszik. A támogatott composer.lock nem kerül ebbe a számlálóba.

A state atomikus, 0600-as, symlinken át nem olvasható, futás közben flock védi. Hiányzó/sérült state új állapot, olvasási hiba látható hiba. last_run_at, last_status, last_findings_hash, notified és catalog_newest_mtime mellett értesítési hash/időpontokat tárol; fájltartalom vagy token nem kerül bele. Függő kézbesítésnél a pending_notification tartalmazza a legfeljebb húsz találatot leíró szűrt, legfeljebb 40 000 karakteres üzenetet, a ténylegesen benne szereplő finding-hasheket és az értesítési okok nyugtamezőit. Így a korábban észlelt találat akkor sem vész el, ha a fájl eltűnik az újrapróbálás előtt. A régi failure_streak/failure_notified mezők nem vezérlik az új értesítési döntést.

BELA csak új találatnál kap helyi POST /api/messages üzenetet, azonos találat hét naponta legfeljebb egyszer. Incomplete első/új reasons halmaza azonnal jelez; azonos, sikeresen jelzett okhalmaz 28 napig elnémul. Az útvonal vagy hibaszám változása nem változtatja meg az okhalmazt. Köztes clean sem nullázza a nyugtát. Error azonnal jelez. A 45 napnál régebbi katalógus havonta egyszer jelez. A régi state első új futása az új nyugta hiánya miatt egyszer jelezhet.

A katalógus alaphelye `~/.claude/tools/bumblebee-threat-intel`, --catalog-dir felülírhatja. Elfogadott sémák: 0.1.0 és 0.2.0, vegyesen is. A friss upstream másolat 15 fájlban 1103 bejegyzést tartalmaz: npm 719, editor-extension 244, rubygems 123, pypi 11, packagist 4, go 2. Hibás tétel/fájl catalog_errors-ben számolva, az ismeretlen sémájú fájlt kihagyja, de a többi katalógust és findingot megtartja; részleges hibánál incomplete. Ha nincs használható tétel, catalog_unusable error. Ismeretlen, szöveges ecosystem nem katalógushiba: unsupported_ecosystem_entries számlálóba kerül, nem kerül az egyeztetőbe. Hiányzó katalógus error. A katalógus frissítése kézi/BELA-feladat ellenőrzött forrásból, külön hálózati jogosultsággal; pusztán az mtime átírása nem frissítés és nem hitelességvizsgálat.

A POST előtti ledger és a siker utáni nyugta mentése külön történik. A pending_notification a POST előtt lemezre kerül. Sikertelen küldés error és nem kap notified nyugtát; a függő üzenet az új futásban elsőbbséget kap. Siker után csak az üzenetben ténylegesen szereplő legfeljebb húsz találat kap notified időpontot; a többi a következő futásban jelez, szükség esetén kézzel azonnal újraindítható. A sikeresen kiküldött outbox törlődik. Ha a futás elején már volt pending, annak sikere után az ugyanabban a snapshotban megmaradt új findingokból egyszer még új outbox készül és küldődik, szintén POST előtti mentéssel. Legfeljebb két küldés történik futásonként, nincs végtelen ciklus. A második küldés hibája a második outboxot hagyja pendingben. A POST 200 csak sorba állítást igazol, nem BELA olvasását. Távoli elfogadás és helyi nyugtamentés közti processzhalál vagy tartós lemezhiba ismételhet üzenetet. Akkor is lehet duplikálás, ha a dashboard már elfogadta a POST-ot, de a válasz hibás/elveszett vagy timeout történt: a küldő nem tudhatja biztosan, hogy a másik oldal átvette-e. A protokollban nincs szerveroldali idempotenciakulcs. A dashboard tokent soha nem naplózza.

Tartósan elutasított pending, azaz poison message kezelése jelenleg operátori feladat. Az ok megvizsgálása/javítása után a függő bejegyzés törölhető:

```sh
/usr/bin/python3 scripts/supply-chain-ack.py clear-pending
```

A parancs a közös lock alatt, atomikusan kizárólag a pending_notification mezőt törli, a többi állapotot és az ack-fájlt változatlanul hagyja. Kiírja a törölt finding-hasheket (legfeljebb húsz, plusz többletszám), az üzenet tartalmát nem. Hibás pending szerkezetet is eltávolíthat, de sérült teljes JSON-t nem ír felül. A közvetlen kézi mezőtörlés helyett ezt érdemes használni. NEM jelöli a findingokat notified-nek: az aktuálisan még jelenlévő finding később újra jelentkezhet. Ezért a kiváltó okot is javítani kell; nem állandó biztonsági kivétel.

A --dry-run sem állapotot, lockot, bytecode-ot, sem ack-et nem ír, és semmilyen HTTP-t nem hív. A --now időzónás ISO vagy Unix-másodperc; jövőbeli tárolt futási idő clock_rollback hiba. A határértékek Unix-idővel számolnak; a timer hétfő 09:00 Europe/Budapest, DST szerint.

**A régi bumblebee-hygiene-scan heartbeatet a timer engedélyezése ELŐTT le kell tiltani.** Patch, a service két /path/to/marveen helyőrzőjének kitöltése, dry run és az ismert hibák egyedi ellenőrzése után telepítheti az operátor a sablonokat. Persistent=true, Restart=on-failure, RestartSec=300, StartLimitIntervalSec=3600, StartLimitBurst=5, TimeoutStartSec=120. Összesen öt próbálkozás (az elsővel együtt), ötperces időközzel, egyórás limitablakban. Tartós dashboard-hiba után a service failed és a pending megmarad: NINCS végtelen háttér-retry. A dashboard helyreállítása után az operátor reset-failed és start paranccsal folytathatja, különben a következő heti timer futása próbálja újra. Ez az automatikus újrapróbálás dokumentált határa; az incomplete nem fogyasztja ezeket a próbákat. Az első futás után journalctl ellenőrzés kell. A leadás nem telepít és nem engedélyez semmit.

## Ellenőrzési eredmények és stresszkör

168 scanner/ack teszt (1,128 s) és 60 közös kanban-teszt (0,831 s), hálózat nélkül. Az új regressziók a mélyen behúzott Yarn version függőséget, wrapperes MCP-indításokat és Windows basename-t, új konfigurációs útvonalakat, editable lockbejegyzéseket, Debian egg-info fájlt, további gemplatformokat és négytagú extension-verziót ellenőrzik.

A stresszkör igazolja: pending után ugyanabban a futásban elküldhető az új finding, de legfeljebb egy további üzenet; második POST-hibánál megmarad a második outbox. Foglalt lock exit 75. A clear-pending megőrzi a többi state-mezőt, nem érinti az ack-fájlt és nem írja ki a poison tartalmát; atomikus íráshibánál megmarad az előző állapot. Az ack feldolgozása valóban lockon kívül történik, a közben változó bájtok hash-ellenőrzése megakadályozza a hibás nyugtamentést. A korábbi 146 teszt továbbra is a csomag része.

Valódi futás az automatikus rendszerkönyvtárakkal, friss katalógussal és külön, ack nélküli scratch state-dirrel:

```sh
/usr/bin/python3 -B scripts/supply-chain-scan.py --catalog-dir /home/kisss/codex-review-marveen/threat-intel-upstream-20261008 --state-dir /tmp/supply-5038-noack --dry-run
```

A teljes kimenet: supply-chain-scan-dry-run-final-noack.json a munkatérben. Mért eredmény: 333 724 fájl, npm 18 368 és PyPI 297 megfigyelés, 0 finding, 1103 katalógustétel, 0 katalógushiba, 2 parse-hiba; status incomplete, exit 0. Falióra 11,83 s, elapsed_ms 11693, csúcsmemória 45 452 KiB. A 278 unsupported lockfile bontása: 73 .package-lock.json (rejtett npm-lock, jelenleg nincs értelmezve), 22 egyéb *-lock.json és 183 egyéb *.lock. Cargo.lock, flake.lock, deno.lock és go.work külön típusként is felismerhető, ebben a mérésben nem volt találatuk. Egyedi ismeretlen fájlnevet nem ír ki; a típuscsoportok nem bizonyítják, hogy minden állomány valódi dependency lockfile. A rejtett npm-lock releváns következő parserbővítés lehet. Két sérült manifest marad: fast-uri és az nvm projekt-fixture rootmanifestje. Nincs katalógus- vagy mélységi hiba. A katalógus kora továbbra is a fájlok mtime-ja, nem publikációs dátum. A dry run nem küldött, nem mentett állapotot, az éles store-t nem használta.

A teljes systemd-analyze --user verify korábban nem fejeződött be a sandbox socketjogosultsága miatt (SO_PASSRIGHTS/SO_PASSCRED Operation not permitted); ezt nem tekintem teljes unit-validálásnak. A service/timer sablon ebben a körben nem változott. Élő dashboard-POST, timer-telepítés és másik WSL/Windows gépen futás nem történt. Nincs commit, push, letöltés vagy éles ack/state-módosítás.
