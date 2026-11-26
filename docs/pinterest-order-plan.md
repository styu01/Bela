# Pinterest kevert sorrend – f91e85da

Az implementáció az aktuális `/home/kisss/marveen/scripts/pinterest-buffer-topup.py`
munkatérbe másolt változatára épül, a videó-, thumbnailOffset- és `--max-add`
kezelés megőrzésével. Az éles könyvtárat nem módosítja. A csomagban lévő
`pinterest-order-plan.patch` review után alkalmazható az éles repóban; ezt
ebben a feladatban nem hajtottuk végre. Nincs telepítés vagy aktiválás.

## Használat

A repó gyökeréből, először kizárólag helyi előnézet:

```sh
python3 -B scripts/pinterest-order-plan.py --image-days 19-22 --video-days 23-26 --dry-run
```

A `--dry-run` nyolcsoros táblát ír, nem hoz létre könyvtárat, tervet vagy lockot,
és nem használ hálózatot. Az órák ET szerint: 8,10,12,14,16,19,21.
A `19,20,21,22` alak is használható. Mindkét oldalon pontosan négy,
szigorúan növekvő, különböző 1–9999 nap kell; a videónap a hozzá tartozó
képnappal +4 kapcsolatban álljon. A két naplista nem fedheti egymást.

Későbbi helyi tervírás ugyanazzal a paranccsal, `--dry-run` nélkül.
Az alapértelmezett hely a repó `store/pinterest-order-plan.json` fájlja;
`--state-dir` segítségével más könyvtár választható. A generátor a topuppal
közös lockot használja, és atomikusan cseréli a tervfájlt. A normál generálás
szándékosan felülírja az előző tervet. Ebben a csomagban kizárólag az
`examples` könyvtárba készült mintaterv; aktív store-ba nem került terv.

```json
{"order":["V25_P1","D19_P1","V25_P2","D19_P2"]}
```

Ez rövid sémapélda. A teljes generált terv 56 különböző kulcsot tartalmaz.
A terv kézzel is készíthető és lehet részleges vagy üres: a topup csak
sorrendi prioritásként használja, nem jóváhagyási listaként.

## A nyolc nap szerkezete

| Posztolási nap | Képek forrásnapja | Videók forrásnapja | Kép/videó |
|---|---|---|---|
| 1 | D19 P1–P3 | V25 P1–P4 | 3/4 |
| 2 | D19 P4–P7 | V25 P5–P7 | 4/3 |
| 3 | D20 P1–P3 | V26 P1–P4 | 3/4 |
| 4 | D20 P4–P7 | V26 P5–P7 | 4/3 |
| 5 | D21 P1–P3 | V23 P1–P4 | 3/4 |
| 6 | D21 P4–P7 | V23 P5–P7 | 4/3 |
| 7 | D22 P1–P3 | V24 P1–P4 | 3/4 |
| 8 | D22 P4–P7 | V24 P5–P7 | 4/3 |

A napokon belül és a naphatárokon is szigorúan váltakoznak a típusok.
Minden forrásnap P1–P7 sorrendje megmarad. D19/V23, D20/V24, D21/V25,
D22/V26 azonos P-jű párjai között 3–5 posztolási nap különbség van.
A videóforrásnapok sorrendje szándékosan 25,26,23,24; növekvő
videónapsorrenddel a kép és saját videója azonos napra kerülne.

## Topup és hibakezelés

- **Hiányzó fájl:** a korábbi működés marad, az eredmény JSON sémája sem bővül.
- **Érvényes terv:** előbb a felsorolt, ténylegesen meglévő jóváhagyott draftok
  következnek tervsorrendben. A többi az eredeti `(nap,pin,típus)` kulcs szerint
  UTÁNUK jön, a Buffer API visszaadási sorrendjétől függetlenül.
- **Üres lista:** minden draft a régi sorrendben marad.
- **Hibás JSON, ismétlődő JSON-mező, hiányzó/idegen mező, duplikált vagy
  ismeretlen alakú kulcs:** `order_plan_invalid`, a topup CLI kilépési kódja 1.
  Nem próbál visszaesni a régi sorrendre, és még Buffer-fetch sem történik.
- **Olvashatatlan fájl, directory, FIFO, symlink (törött symlink is):**
  `order_plan_read_failed`, kilépés 1. A FIFO sem blokkolhatja a folyamatot.
- Legfeljebb 1 MiB fájl és 10000 kulcs fogadható el.
- A kanonikus kulcs `D19_P1` vagy `V23_P1`. Nulla, vezető nulla, suffix,
  kisbetűs típus és nem ASCII szám a tervben hibás. A Buffer meglévő
  `D019_P01_STRATEGY` alakját a topup továbbra is a `(19,1,D)` azonossághoz
  rendeli; két ilyen azonosság a draftok között továbbra is hibát okoz.
- **A tervben szereplő, de sem draftként, sem scheduled posztként nem látható
  kulcsok** az `order_plan_missing` eredménylistába kerülnek. Ez nem végzetes
  hiba: publikálás után a Buffer természetesen eltávolítja őket a két
  lekérdezett státuszból. A hiányzó kulcs önmagában nem különbözteti meg az
  elírást vagy még fel nem töltött draftot a már publikált pintől. Emiatt a
  topup a jelenlévő pineket rendezi, és a hiányt kifejezetten jelzi; nem talál
  ki draftot és nem hagy el egyéb érvényes draftot. Az üzemeltető ellenőrizze
  ezt a listát az első előnézetnél. Az érvényes alakú, de nem létező kulcs
  tehát sem marad észrevétlen.
- A korábbi, UTM alapján nem azonosítható Buffer-draftok kezelése változatlan.
  A tervben viszont nincs ismeretlen mező vagy hibás kulcs csendes eldobása.
- A szüneteltetett topup változatlanul visszatér még a terv olvasása előtt.

A hibakódok rögzített szövegek: fájltartalom, útvonal vagy nyers kivétel nem
kerül a hibaüzenetbe. A meglévő értesítési és napi deduplikálási szabályok
változatlanok. A generátor hibái: `order_plan_days_invalid`, `busy`,
`lock_failed`, `state_write_failed`; hibás CLI-szintaxis argparse kódja 2.

## A naptári garancia határa

Ez egy sorrendterv, nem dátumhoz kötött kampány. A nyolc teljes napos tábla
hiánytalan 56-os készlet és hét napi slot mellett teljesül, ha az első
tervtétel egy teljes ET-nap első szabad slotjára kerül. A topup a meglévő
sor végét folytatja, a jelenlegi posztokat nem mozgatja. Napközi indítás,
egyéb posztok, hiányzó draft, kevert tartalmú korábbi queue, megváltoztatott
slotszám vagy napi cap esetén csak a megmaradó tételek sorrendje garantált;
a napi arány és a kép/videópár naptári különválása nem feltétlenül marad meg.
A 10-es Buffer-limit sem kerüli meg a napi slotokat: a szkript több futáson
át tölti fel a készletet. Az eredeti 14 napos horizont és `--max-add` érvényes.

## Ellenőrzés

```sh
python3 -B scripts/__tests__/run-pinterest-tests.py
```

A tesztfuttató socket-szintű hálózati tiltással futtatja a régi három
tesztfájlt és az új stressztesztet. Minden Buffer/dashboard művelet fake;
a videó-időzítés külön helyi ffprobe tesztjei a korábbi csomag részei.
A tesztek és a mintafuttatások nem olvasnak éles kulcsot, és nem hívnak
éles Buffer API-t. A topup valódi `--dry-run` módja önmagában továbbra is
végezne Buffer-LEKÉRDEZÉST; ilyen futtatást itt nem indítottunk.

A részletes eredmények és a review célpontjai: `self-stress-report.md`.
