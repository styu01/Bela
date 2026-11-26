# Elavult weboldalak - woowebsite.eu ügyfélszerzés (folyamatos projekt)

Indítva: 2026-09-03, Istvan explicit kérésére. Kanban: 4caa439d.

## Cél

Hosszútávú, ismétlődő lead-generálás woowebsite.eu számára: magyar kisvállalkozói
weboldalak felkutatása, amik elavultak/sériltek/technikailag gyengék, és
outreach-alapot adnak a megkereséshez.

## Futtatási mód

- **On-demand**, NEM ütemezve -- csak amikor Istvan kéri ("fusson").
- Batch méret: **10 jelölt/kör**.
- Nem csak Top 5 szűkítés -- Istvan MIND a 10-re részletes profilt kér.

## Régió-sorrend (gazdasági rangsor alapján, WebSearch 2026-09-03, ld. Portfolio.hu/HVG/KSH)

Gazdagabb megyék előre, majd minden régió sorban:

1. Budapest + Pest megye (agglomeráció)
2. Komárom-Esztergom megye
3. Fejér megye
4. Veszprém megye
5. Tolna megye
6. Győr-Moson-Sopron megye
7. (a többi megye ezután, sorrend még nyitott -- a maradék: Vas, Zala, Baranya,
   Somogy, Bács-Kiskun, Csongrád-Csanád, Békés, Hajdú-Bihar, Jász-Nagykun-Szolnok,
   Borsod-Abaúj-Zemplén, Heves, Nógrád, Szabolcs-Szatmár-Bereg -- utóbbi az
   ország leggyengébb vásárlóerejű megyéje, ezért legutolsóra hagyjuk)

## Profil-mezők minden jelöltnél (Istvan 2026-09-03 kérése alapján)

- Cég neve, URL
- Platform: WordPress / Joomla / Drupal / sima HTML / egyéb (azonosítva, nem
  feltételezve -- pl. generator meta tag, wp-content útvonal, jellegzetes
  markup)
- Elavultság konkrét jelei: SSL hiány, régi copyright év, hiányzó meta
  title/description, nem mobilbarát/viewport hiány, halott social linkek,
  törött képek/linkek, encoding hiba, ECONNREFUSED/site down
- Gyengeségek összefoglalva (mit mondanál nekik outreach-ben)
- Elérhetőség: telefon, email (ha nincs közvetlenül az oldalon, WebSearch a
  cégnévre)

## Kör-napló

(minden lezárt batch ide kerül: dátum, régió, hány jelölt, hova lett kiküldve)

- 2026-09-03: projekt elindítva, első kör (Budapest/Pest, 10 jelölt) elakadt
  (nyers curl loop miatt WSL-lefagyás, ld. cold_wsl_freeze_raw_curl_root_cause_20260903
  memória), skill javítva (kötelező quarantine-reader/WebFetch).
- 2026-09-03: 1. kör újraindítva és lezárva a javított módszertannal (kizárólag
  quarantine-reader, semmi raw curl, nem volt akadás/gyanús viselkedés). 10
  jelölt Budapest/Pest megyéből (kb. 24 gyűjtött, 21 fetchelt, 11 elvetve
  konkrét okkal), kiküldve Istvannak Telegramon. Öt jelölt HuPont.hu ingyenes
  weblapépítőn fut. Nem volt ECONNREFUSED/nincs-SSL ebben a körben. Módszertani
  megjegyzés: néhány "hiányzó meta/viewport" megállapítás a quarantine-reader
  feldolgozott markdown-kimenetén alapul, nem 100%-ban nyers forráskód-ellenőrzés.
  Következő kör: Komárom-Esztergom megye.
- 2026-09-23: Istvan pontosított -- Budapest/Pest megyéből NEM elég 10 jelölt
  (több ezer weboldal van a régióban), legalább 200 kell első körben.
  Delegálva `gemini/kutato`-nak (msg #2760). ELSŐ TERV (fájlírás
  gemini oldalán) MEGHIÚSULT: gemini/kutató jelezte (msg #2769), hogy a
  szerepköre kizárólag kutatás/elemzés, fájlírásra/módosításra NINCS
  jogosultsága -- a `/home/bridge/.gemini/antigravity-cli/...` útvonal (amit
  korábban artifact-linkként küldött) egy teljesen elkülönített sandbox, a
  BÉLA-oldali fájlrendszerről egyáltalán nem elérhető (`ls /home/bridge/`
  "No such file or directory"). A `/home/kisss/gemini-shared/` közös
  könyvtárba SEM tud írni.
  JAVÍTOTT MÓDSZER (msg #2770): gemini/kutató kis adagokban (max 3-5
  találat/üzenet) KÜLDI vissza a találatokat inter-agent üzenetben, BÉLA írja
  ki egyenként .txt fájlba a `/home/kisss/gemini-shared/broken-site-audit-budapest-pest-200/`
  könyvtárba, ahogy megérkeznek -- ez tartja meg az eredeti célt (ha gemini
  kerete elfogy menet közben, a már elküldött adagok ne vesszenek el), csak a
  fájlírás felelőssége BÉLA-nál van, nem gemininél. Napi állapot-ellenőrzés
  beütemezve (`broken-site-audit-bp-pest-gemini-checkin`, 10:00 CEST).
