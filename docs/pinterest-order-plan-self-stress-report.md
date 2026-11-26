# Codex self-stress-test – f91e85da

**Codex self-stress-test kész.** 2026-11-13, minden teszt offline.

Eredmény: **161 teszt, OK** (8,738 s a végső, patchből telepített futásban).
Nem történt éles Buffer-hívás, éles dry-run, telepítés vagy aktiválás.

## Ellenőrzött esetek

- Módosítás előtti alapmérés: 60 topup + 61 korábbi stressz + 10 schedule =
  131 sikeres teszt. A Python unittest fájlfelfedezés a pontozott fájlnevek
  miatt 0 tesztet talált; ezért explicit modulbetöltéses futtató készült.
- A végső csomag: ugyanaz a 131 teszt, plusz 30 új teszt. A korábbi stressz
  egyetlen elvárását módosítottuk: a régi hibakódkészlethez pontosan a két
  új order-plan kód adódik. A régi fixture érintetlen.
- 1000 determinisztikusan véletlen forrásnapblokk: 56 különböző kulcs,
  28 kép+28 videó, váltakozó 3/4–4/3 napi arány, teljes típusváltakozás,
  forrásnaponként P1–P7, minden D→V +4 pár eltérő posztolási napra kerül.
- 150 kevert draftlista eltérő terv-részhalmazokkal és strategy suffixekkel:
  minden tervezett draft megelőzi a többit; utána a régi numerikus sorrend.
- 100 differenciális összehasonlítás a módosítás előtti aktuális topuppal:
  terv nélkül azonos JSON és mutation payload, változó készlettel,
  visszaadási sorrenddel, 0–11 scheduled poszttal és `--max-add` értékkel.
- Teljes 8 napos szimuláció, óránkénti topup, publikált posztok eltávolítása,
  folyton összekevert draftok, 56 valódi helyi pin.txt-fixture, fake Buffer.
  Egyszerre soha több mint 10 scheduled; pontosan az 56 tervezett pin,
  8 teljes ET-nap, 8/10/12/14/16/19/21 slot, helyes napi arány és párelválasztás.
- Hiányzó fájl, üres terv, sérült JSON/UTF-8, túl nagy fájl/lista, hibás
  schema/típus, duplikált JSON-mező/kulcs, vezető nulla, suffix, Unicode szám,
  szélső nap/pin, directory/FIFO/symlink, jogosultsághiba.
- Hiányzó draft explicit JSON-diagnosztikája, már scheduled kulcs,
  teljesen kifutott terv és új készlet, később szabálytalan sorrendben
  érkező új draftok, duplikált Buffer-azonosság, hiányzó pin.txt.
- Buffer-limit 0–11 már scheduled tételnél; `--max-add=1`; teljes batch
  preflight; részleges mutation hiba és következő futás helyes folytatása.
- Generátor dry-run nem hoz létre állapotkönyvtárat. Topup dry-run fake
  Bufferrel nem módosít draftot, state-et, lockot vagy értesítést.
- Közös flock, atomikus tervcsere, hibás replace után régi terv megmarad,
  ideiglenes fájl eltakarítva. Nincs tetszőleges nyers kivételszöveg a CLI-n.
- Az átadott patch friss ideiglenes könyvtárban `git apply --check` és
  alkalmazás után is ellenőrzött. Pontosan ebből futott újra a 161 teszt,
  majd a generátor dry-runja. Nem keletkezett store vagy Python bytecode.

Az első stresszkörben egy túl szigorú teszt és magyarázat pontosan négy
napnyi párelválasztást követelt. A helyes távolság 3–5 nap; a szükséges
eltérő nap változatlanul teljesült. Ezt javítottuk, a végső kör mind zöld.

## KISPROGI-review célpontjai

1. Terv nélküli változatlanság és a tervhez hozzáadott rendezés/hibakódok.
2. Hiányzó plan-tételek explicit diagnosztikája. Ez tudatosan nem végzetes:
   a publikált tételek eltűnnek a Bufferből. A listából önmagában nem
   állapítható meg, hogy egy tétel publikált vagy még hiányzik a draftokból.
3. A naptári arány feltételei: a sorrendterv nem rendezi át a régi queue-t;
   részleges nap vagy hiányzó készlet mellett csak a sorrend garantált.
4. Kanonikus tervkulcs és Buffer UTM-normalizálás, duplázás elleni védelem.
5. Közös lock/atomikus írás, hiányzó vs. sérült/olvashatatlan terv különbsége.

Az átadott patch az aktuális éles topuphoz és stresszteszthez képest készül;
a mellékelt `scripts/__tests__/fixtures/pinterest-buffer-topup-before-order.py` a differenciális méréshez
rögzített, módosítás előtti példány. A csomag többi már meglévő segédfájlja
csak a tesztek önálló futtatásához szükséges, a patch nem írja felül őket.
