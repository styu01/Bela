# Codex kvótakijelzés: megfigyelés és utolsó ismert állapot

A kollektor továbbra is csak helyi rollout-*.jsonl fájlok allowlistelt rate_limits mezőit olvassa, legfeljebb nyolc fájlból, módosítási idő szerinti sorrendben. A fájl neve és a könyvtár dátuma a munkamenet létrehozását jelöli; egy november 7-i session november 9-én is frissülhet. A kvóta frissességét az esemény timestamp mezője dönti el, nem a fájlnév, az mtime vagy a lekérdezés ideje.

A 2026-11-09-i vizsgálatban nem igazolódott forrás- vagy formátumváltás. A régi parser a friss, limit_id=codex eseményeket elfogadta, a 300 és 10080 perces ablakokkal. A korábbi 08:16:18.991 UTC és az új 14:16:25.908 UTC esemény között 21606.917 másodperc volt. A thread_history_1.sqlite időmetaadatai szerint 08:17 és 14:15 UTC között új forduló nem kezdődött, az előző utoljára lezárt forduló 08:16:19 UTC-kor ért véget. A vizsgálatkor indult új forduló új, érvényes kvótaadatot írt ugyanabba a session-fájlba. A logs_2.sqlite-ben a rateLimits értesítések neve szerepelt, a friss, felhasználható százalékok a sessionnaplóban voltak.

## Megmaradó és javított szabályok

- FRESH_SECONDS változatlanul 1800 másodperc. A generated_at a gyűjtés ideje; az observed_at a valódi kvótaesemény ideje. Régi adatot nem nevezünk frissnek.
- A kollektor új observation_age_seconds mezője az esemény korát jelzi a gyűjtéskor, hiányzó/jövőbeli időnél null. A checker mindig önállóan újraszámítja a kort observed_at-ból, így a mező vagy generated_at manipulálása nem frissíti fel a régi értéket.
- A 30 percnél régebbi, érvényes megfigyelésből a checker nyitott ablakban „legalább X% (nem friss)” alsó korlátot és mérési kort ír. Történeti, nyitott ablakban 90%-tól „legalább X% (STOP-szint, nem friss)”, 80%-tól 90% alatt „legalább X% (OPATLAN-szint, nem friss)” jelenik meg. 80% alatt a jelzés semleges, OK címke nincs. A régi érték alsó korlát, így az elért figyelmeztetési szint resetig továbbra is bizonyított. Nem állítja bizonyíték nélkül, hogy a kliens inaktív: a régi megfigyelés más gyűjtési problémát is jelenthet.
- Ha valamelyik ablak resetideje már elérkezett, az aktuális használat ISMERETLEN; a régi százalék külön, utolsó ismert értékként látható. Nem számítunk ki automatikus 0%-ot, mert más kliens/eszköz is fogyaszthat ugyanabból a keretből. A másik ablak ettől még megjelenhet.
- Friss adatnál OK <80, OPATLAN 80..<90, STOP >=90 változatlan. Nyitott ablakban a korábbi használat csak alsó korlát, ezért történeti adatból nincs „OK, dolgozhatsz” jelzés. Reset után a korábbi százalék már nem alsó korlát: csak előző mérésként jelenik meg, aktuális ISMERETLEN mellett, státuszcímke nélkül.
- Hiányzó, hibás, nem támogatott ablakméretű vagy jövőbeli megfigyelés továbbra is ISMERETLEN. Nincs 300/10080 perces ablakokra találgatás vagy különböző eseményekből összerakott kvóta.
- Sikeres gyűjtés friss és régi, érvényes adatnál is rc=0 és csendes stdout. Az idle cronfutások nem növelik a naplót. A snapshot freshness és observation_age_seconds mezője, illetve a checker korjelzése mutatja a frissességet. rc=0 önmagában nem jelenti, hogy friss a kvóta. Hibánál a fix üzenet továbbra is kiíródik.
- Forráshiánynál rc=1; meglévő snapshotot megőrzi. Első, hiányzó forrású futás missing snapshotot hoz létre rc=1 mellett.
- A CODEX_HOME környezeti változó elsőbbséget kap; enélkül a Unix-felhasználó .codex könyvtára az alap. A cron által adott HOME nem irányít át másik fiókra. --sessions-dir mindkettőt felülírja.
- Nincs auth.json olvasás, közvetlen kvóta-API vagy modellhívás. A parser beszélgetést, eszközkimenetet és tetszőleges payload-mezőt nem exportál. A script és függvénybetöltése bytecode-cache-t sem ír.

Hivatalos OpenAI dokumentáció a rate-limit eseményekről és a reset másodperces egységéről: https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt . A helyi snake_case sessionformátumot a tényleges naplóban és a parserben ellenőriztem. Az account/rateLimits/read RPC-t nem hívtam meg.

## Használat

```sh
python3 -B scripts/codex-usage-collect.py
python3 -B scripts/codex-usage-check.py
# Izolált kimenet valódi, csak olvasott sessionadatból:
python3 -B scripts/codex-usage-collect.py --output /tmp/codex-quota-review.json
python3 -B scripts/codex-usage-check.py --input /tmp/codex-quota-review.json
```

## Ütemezés és átadás

**A `*/10` cron már él, nincs telepítési teendő.** KISPROGI önálló ellenőrzése szerint a meglévő crontab tízpercenként futtatja a kollektort:

```cron
*/10 * * * * python3 /home/kisss/marveen/scripts/codex-usage-collect.py >> store/codex-usage-collect.log
```

Nem kell új systemd service vagy timer, a csomag ezekhez mintát sem tartalmaz. Az előző körben hozzáadott Codex unitmintákat eltávolítottam, hogy ne induljon párhuzamos második gyűjtő. A cron és a systemd külön ütemező; a systemd unitok hiánya nem bizonyította a gyűjtés hiányát. A korábbi telepítési javaslatot ez a szakasz felülírja.

A cron tízpercenként újraolvassa az aktív session kvótaeseményeit. Ha nincs új megfigyelés, az observed_at változatlan és a valódi kora nő. A sikeres idle futás csendes, a freshness nem válik mesterségesen fresh-sé. Hiányzó forrás és nem létező snapshot esetén missing JSON készül és rc=1, fix hibaüzenettel. Meglévő snapshotot hiányzó forrás nem ír felül; ekkor is rc=1 és hibaüzenet jár. A hiba miatti ismételt naplósor szándékos, normál idle állapotban nincs naplóáradat.

A jelen módosítás munkapéldány, alkalmazását KISPROGI review követi. Új ütemező telepítése vagy aktiválása nem szükséges. Itt crontab-módosítás, unittelepítés, daemon-reload, restart, deploy, commit vagy push nem történt. Az éles snapshotot sem frissítettem; a valódi ellenőrzés kimenete csak a munkapéldányba került.

A cron adata KISPROGI közölt, önálló ellenőrzéséből származik. Az ebben a körben megkísérelt saját crontab -l sandboxban jogosultsági hibát kapott, a hozzá kért engedélyt elutasították, ezért saját sikeres crontab-lekérdezést nem állítok.
