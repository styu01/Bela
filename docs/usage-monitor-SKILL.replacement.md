---
name: usage-monitor
description: Claude 5 órás és heti keret ellenőrzése, külön 90/98 és 80/90 küszöbbel, tartós deduplikációval
---

# Claude-keret ellenőrzése, István 2026-11-07 döntése

Az operátor ezt a leírást a régi helyére alkalmazza; ne indítson mellé második aktív monitorütemezést. Ha a modellmentes systemd timer már aktív, a régi scheduled taskot le kell tiltani, nem párhuzamosan futtatni.

Minden tényleges usage-monitor futásban egyszer hajtsd végre:

```text
python3 /home/kisss/marveen/scripts/usage-fleet-monitor.py
```

A `usage-monitor-shortcircuit.py` nem előzheti meg ezt a lépést: változatlan usage/pause mellett is szükség lehet pending nyugta vagy Telegram-várólista folytatására. A monitor saját maga zárol, deduplikál és csak az engedélyezett helyi forrásokat olvassa. A usage-collect.py adatgyűjtőt ne indítsd újra, ne olvass kulcsot/tokent, ne végezz kézi API-lekérdezést.

A Claude-szabály:

- 5 órás keret: 90%-tól óvatos figyelmeztetés, a munka folytatható; pause csak >=98%.
- Heti keret: 80%-tól figyelmeztetés, pause >=90%.
- Már elrendelt pause feloldása: 5 órás <90% ÉS heti <80%. Pontosan 90% vagy 80% még tartja a megfelelő hiszterézist.
- A Codex és Gemini külön keret. E Claude-folyamat ezekre nem rendel el szünetet.

A script kezeli a pause-fájlt, az ágensoldali pending nyugtákat, a high-water jelzéseket és a Telegram legfeljebb három kísérletes várólistáját. A `level=0/80/90` fáziskód, nem egységes százalékküszöb. A régi 90-es fázis már elrendelt szünet: friss, mindkét feloldási feltételt teljesítő adat nélkül nem oldható fel.

Az ágens- és owner-üzenet mindig nevezze meg a Claude-keretet, az érintett 5 órás/heti ablakot, a százalékot és az ismert resetidőt. Feloldásnál mindkét ablak szerepeljen. Ékezetes magyar szöveg, gondolatjel, em dash és dupla kötőjeles elválasztó nélkül. A script ezt előállítja; ne küldj második, saját figyelmeztetést vagy feloldást.

STALE esetén nincs usage-üzenet vagy kézi feloldás. RETRY esetén hagyd meg a tartós várólistát a következő futásnak. ERROR esetén csak a fix hibakódot jegyezd fel, ne írj ki nyers kivételt, API-választ vagy credentialt. Egyik esetben se írd át kézzel a pause- vagy ledgerfájlokat. Nincs külön második véleménykérés a százalékok alapján: a script az elfogadott szabályt determinisztikusan alkalmazza.

# A meglévő projektstátusz-ellenőrzés

A projektstátusz rész korábbi rövidzárát külön tartsd meg. Kizárólag a fenti monitorfutás UTÁN futtasd:

```text
python3 /home/kisss/marveen/scripts/usage-monitor-shortcircuit.py
```

USAGE_MONITOR_SKIP esetén csak az alábbi projektstátusz-részt hagyd ki. USAGE_MONITOR_PROCEED esetén folytasd. Ez nem hagyhatja ki vagy állíthatja le az előző, pending-várólistát is kezelő monitorlépést. A projektellenőrzés eredeti, usage-változáshoz kötött ritmusa így megmarad.

Ellenőrizd az OKOSKA ÉS PROGI fejlesztési folyamatot is: van-e új inter-agent üzenet amit feldolgozni kell (curl -s -H "Authorization: Bearer $(cat /home/kisss/marveen/store/.dashboard-token)" http://localhost:3420/api/messages?agent=bela). Ha PROGI mérföldkő-eredménnyel jelentkezett a Béla Home v2 vagy az okosugyintezo.hu projektben, nézd át röviden a leírt munkát, majd jelezd Istvannak Telegramon tömören hol tart a projekt.
