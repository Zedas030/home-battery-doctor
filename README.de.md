# Battery Doctor (Akku-Doktor)

**Verfolgt die Gesundheit der Heimspeicher über Monate und Jahre – herstellerunabhängig, aus den Daten, die Home
Assistant ohnehin aufzeichnet.** Der Akku-Doktor misst die echte nutzbare Kapazität mit Konfidenzbereich, zählt
Vollzyklen, zeigt Alterungstrend und Prognose, analysiert gestapelte Master/Slave-Einheiten, überwacht die Garantie und
erstellt auf Knopfdruck ein Garantie-Dossier (PDF + Rohdaten).

> English documentation: [README.md](README.md)

**Der Akku-Doktor liest nur.** Er steuert keinen Speicher, ändert keine Ausgangsleistung und keine Automation. In
Home Assistant schreibt er ausschließlich seine eigenen Anzeige-Sensoren (`sensor.akku_doktor_*` bzw.
`sensor.battery_doctor_*`) und schickt Benachrichtigungen. Jeder andere Dienstaufruf wird im Code abgelehnt, bevor
er das Netzwerk erreicht (`akd/ha.py`, per Test abgesichert).

Entstanden ist er für einen Balkonspeicher aus zwei gestapelten Growatt NOAH 2000 (Master mit Wechselrichter +
Erweiterungsakku, Daten über [noah-mqtt](https://github.com/mtrossbach/noah-mqtt)). Er funktioniert aber mit jedem
Speicher, der in Home Assistant einen Ladestand sowie Lade- und Entladeleistung liefert.

---

## Inhalt

1. [Schnellstart](#schnellstart)
2. [Secrets: Token eintragen und wechseln](#secrets-token-eintragen-und-wechseln)
3. [Konfiguration](#konfiguration)
4. [Was du bekommst](#was-du-bekommst)
5. [So wird gerechnet](#so-wird-gerechnet)
6. [Master/Slave-Analyse](#masterslave-analyse)
7. [Garantie](#garantie)
8. [Selbstüberwachung und Meldungen](#selbstüberwachung-und-meldungen)
9. [Daten, Aufbewahrung, Sicherung](#daten-aufbewahrung-sicherung)
10. [Befehle](#befehle)
11. [Tests](#tests)
12. [Fehlerbehebung](#fehlerbehebung)

## Schnellstart

Voraussetzungen: Docker mit Compose, Home Assistant mit den Speicher-Entitäten, optional Prometheus mit dem
HA-Exporter für feine Historie.

```bash
git clone https://github.com/Zedas030/home-battery-doctor.git
cd home-battery-doctor
cp config/config.example.de.yaml config/config.yaml   # deutsche Vorlage, anpassen (siehe unten)
cp .env.example .env && chmod 600 .env                 # Token eintragen, siehe "Secrets"
docker build --target test .                            # Tests laufen lassen (optional, ~1 min, auf dem Pi ~5 min)
docker compose up -d --build
docker exec battery-doctor python -m akd selftest
```

Weboberfläche: `http://<host>:8104`. Beim ersten Start liest der Akku-Doktor die vorhandene Historie ein (Prometheus
minutengenau, HA-Recorder, HA-Langzeitstatistik stündlich) und wertet sie aus. Das dauert auf einem Raspberry Pi
einige Minuten. Aus dieser Historie werden keine Meldungen verschickt, damit es keine Push-Flut gibt.

Autostart: `restart: unless-stopped`. Ressourcen: 384 MB RAM, 0,5 CPU. Logs: höchstens 3 × 10 MB.

## Secrets: Token eintragen und wechseln

Es gibt genau ein Geheimnis: **`HA_TOKEN`**, ein langlebiges Zugriffstoken von Home Assistant.

- **Wo:** in der Datei `.env` neben `docker-compose.yml`, Rechte `600`. Nicht in `config.yaml` und nie im Git
  (`.env` steht in `.gitignore`).
- **Anlegen:** In Home Assistant links unten auf dein Profil → Reiter **Sicherheit** → **Langlebige Zugriffstoken** →
  **Token erstellen**, Name z. B. „Akku-Doktor“. Den angezeigten Wert in `.env` eintragen: `HA_TOKEN=...`
- **Wechseln/rotieren:** Neues Token wie oben anlegen, in `.env` ersetzen, dann `docker compose up -d` (liest `.env`
  neu). Danach das alte Token in HA unter „Langlebige Zugriffstoken“ löschen.
- **Prüfen:** `docker exec battery-doctor python -m akd selftest` meldet „token rejected (401)“, wenn es nicht passt.
- Weitere Einträge in `.env` sind optional und nicht geheim: `AKD_BACKUP_DIR` (Ziel der Sicherung) sowie
  `AKD_UID`/`AKD_GID` (Benutzer des Containers, z. B. `1000`, wenn das Sicherungsziel eine für uid 1000 eingehängte
  CIFS-Freigabe ist).

## Konfiguration

Alles steht kommentiert in `config/config.yaml` (deutsche Vorlage `config/config.example.de.yaml`, englische
`config/config.example.yaml`). Nach Änderungen: `docker compose restart battery-doctor`. Wenn du Schätz-Parameter
änderst, danach alles neu auswerten lassen: `docker exec battery-doctor python -m akd analyze`.

Die Schlüssel sind englisch. Die deutschen Schlüssel der Version 1.0 (`einheiten`, `schaetzung`, `garantie` …)
werden weiterhin verstanden.

| Abschnitt | Bedeutung |
|---|---|
| `language` | `de` oder `en`: Meldungen, Weboberfläche, Berichte, Dossier, Namen der HA-Sensoren |
| `system` | Gesamt-SOC und **Gesamt**-Lade-/Entladeleistung (gestapelte Einheiten melden Leistung oft nur gemeinsam) |
| `system.battery_count` | optional: Anzahl gemeldeter Akkus; Fenster zählen nur, solange alle Einheiten dabei sind |
| `units` | je Einheit: SOC, Temperatur, Nennkapazität, Inbetriebnahme (= Garantiebeginn), Seriennummer |
| `warranty` | Garantiebedingungen des Herstellers (Vorlage: Growatt NOAH 2000) |
| `estimation` | Regeln für saubere Messfenster und für „keine verlässliche Aussage“ |
| `master_slave` | optional: Einheiten, Drossel-Status einer bestehenden Automation, Datum einer Umverkabelung oder `auto` |
| `monitoring` | Abfrageintervall, ab wann „keine Daten“ gemeldet wird, Ruhezeit |
| `home_assistant.notify` | Meldedienst, z. B. `notify.mobile_app_handy` |

Ein einzelner Akku geht auch: eine Einheit, kein Abschnitt `master_slave`.

## Was du bekommst

**Weboberfläche** (`:8104`): Übersicht je Speicher (Kapazität ± Bereich, SOH, Vollzyklen, Trend, Garantie),
Kapazitätsverlauf, Master/Slave, alle Messfenster mit Grund für Verwerfen, Selbstüberwachung (jede Entität mit Alter
des letzten Werts, Einheit, Integration), Berichte, Dossier-Knopf, CSV-Export.

**Sensoren in Home Assistant** (werden alle 5 min neu gesetzt; nach einem HA-Neustart sind sie nach spätestens
5 min wieder da). Mit `language: de`:

| Sensor | Inhalt |
|---|---|
| `sensor.akku_doktor_status` | ok / warnung / kritisch / fehler, offene Meldungen als Attribut |
| `sensor.akku_doktor_<bereich>_kapazitaet` | nutzbare kWh, Attribute Unter-/Obergrenze, Messfenster, Grund bei „unknown“ |
| `sensor.akku_doktor_<bereich>_soh` | % der Nennkapazität |
| `sensor.akku_doktor_<bereich>_vollzyklen` | Vollzyklen-Äquivalente seit Datenbeginn |
| `sensor.akku_doktor_<bereich>_prognose_80` | Datum „80 % voraussichtlich“, Text als Attribut |
| `sensor.akku_doktor_<bereich>_garantie` | Garantiestatus, Hinweise, Referenzwert des Herstellers |
| `sensor.akku_doktor_noah_soc_differenz` | SOC-Differenz Master−Slave heute (Tagesmittel) |
| `sensor.akku_doktor_noah_aufwachverzug` | Minuten, die der Slave morgens nach dem Master aufwacht |
| `sensor.akku_doktor_noah_umstecken_effekt` | Änderung der Differenz durch das Umstecken, mit Bereich und Fazit |

`<bereich>` ist `system` oder der Schlüssel einer Einheit (z. B. `master`, `slave`). Steht ein Wert auf `unknown`,
gibt es **keine verlässliche Aussage**; der Grund steht im Attribut `grund`. Mit `language: en` heißen die Sensoren
englisch (`sensor.battery_doctor_system_capacity` …, siehe README.md).

**Prometheus:** `http://<host>:8104/metrics` (Präfix `akd_`). Scrape-Job:

```yaml
  - job_name: battery_doctor
    scrape_interval: 60s
    static_configs:
      - targets: ['localhost:8104']
```

**Grafana:** `grafana/battery-doctor.de.json` importieren oder in den Provisioning-Ordner legen. Neu erzeugen:
`python grafana/make_dashboard.py --lang de`.

**Monatsbericht:** am 1. um 09:00 als HTML (dauerhaft gespeichert), dazu eine Kurzfassung als Push.

**Garantie-Dossier:** Weboberfläche → „Berichte & Dossiers“ → „Dossier erstellen“ (oder
`python -m akd dossier master`). Ergebnis: PDF mit Ergebnis, Kapazitätsverlauf, Monatswerten, Temperaturnachweis,
Methode, Datenquellen und allen verwendeten Messfenstern, dazu ein ZIP mit dem PDF, allen Rohdaten als CSV (Minuten,
15 Minuten, Stunden, Fenster, Schätzungen, Tageswerte) und SHA-256-Prüfsummen.

## So wird gerechnet

Ziel: die **nutzbare** Kapazität aus dem normalen Betrieb messen, ohne Testzyklen und ohne dem BMS zu glauben.

1. **Minutenraster.** Alle Werte werden auf ein Minutenraster gelegt. Ein Wert gilt nur kurz weiter (Leistung 4 min,
   SOC 11 min, weil viele Cloud-Integrationen Akkudetails nur alle 5 min liefern). Liefert die Cloud nichts Neues,
   zeigen HA und Prometheus aber weiter den alten Wert, wird das erkannt: Alle Leistungen bleiben 20 Minuten lang
   exakt gleich **und** der SOC bewegt sich nicht, obwohl er sich laut angezeigter Leistung um mindestens
   2 Prozentpunkte hätte ändern müssen. (Konstante Leistung allein reicht nicht – nachts liefert der Speicher bei
   fester Ausgangsleistung wirklich identische Werte.) Eine schlafende Einheit mit **0 %** zählt als „keine Daten“,
   nicht als leer. Unmögliche Temperaturen (Fehlwert −273 °C sowie 0 °C bei gleichzeitig 0 % SOC) werden verworfen.
2. **Messfenster.** Ein Fenster ist ein zusammenhängender Zeitraum, in dem nur entladen (bzw. nur geladen) wird. Es
   beginnt und endet **genau an einem Wechsel des ganzzahligen Gesamt-SOC**. Damit ist der SOC an beiden Rändern bis
   auf die Stufenlage bekannt, und der Rundungsfehler der ganzen Prozent fällt weitgehend heraus.
3. **Sauber oder nicht.** Verwendet wird ein Fenster nur, wenn alle Bedingungen gelten:
   - mindestens 30 Prozentpunkte Hub und mindestens 30 Minuten Dauer
   - keine Lücke über 10 min (kürzere werden mit dem gehaltenen Wert überbrückt und gehen in die Unsicherheit ein)
   - kein SOC-Sprung über 3 Prozentpunkte (BMS-Kalibrierung, Aufwachen einer Einheit)
   - Ränder nur zwischen 3 und 98 % SOC (oben und unten kalibriert das BMS)
   - Akkutemperatur zwischen 10 und 35 °C
   - SOC läuft nur in Flussrichtung
   - SOC wird nicht gröber als in 5-%-Stufen gemeldet
   - alle Akkus sind gemeldet (optional `battery_count`)
   - Ergebnis plausibel (30–160 % der Nennkapazität)

   Jedes verworfene Fenster ist mit Grund sichtbar (Weboberfläche „Messfenster“, `windows.csv`).
4. **Je Fenster:** Kapazität = Σ(Leistung × Zeit) / ΔSOC × 100. Die Unsicherheit eines Fensters setzt sich so
   zusammen:
   - Stufenlage an beiden Rändern: Stufe/√12 je Rand
   - zufälliger Messfehler der Leistung: 2 %
   - Anteil überbrückter Lücken
5. **Je Zeitraum** (aktuell: letzte 60 Tage; außerdem je Kalendermonat):
   - Ausreißer werden robust verworfen (mehr als 3,5 robuste Standardabweichungen vom Median).
   - Ergebnis ist das gewichtete Mittel (Gewicht 1/σ²).
   - Der 95-%-Bereich kommt aus einem Bootstrap über die Fenster, mindestens aber aus der Fensterunsicherheit.
   - Dazu kommt eine mögliche **gemeinsame Kalibrierabweichung des Leistungssensors (±3 %)**. Sie verschiebt alle
     Werte gleich, ändert also den Trend nicht, zählt aber für die absolute Kapazität und die Garantiegrenze.
6. **Ehrlich bleiben.** „Keine verlässliche Aussage“ gibt es in diesen Fällen, jeweils mit Grund:
   - weniger als 5 saubere Fenster
   - Bereich breiter als ±8 %
   - SOC nur in groben Stufen
7. **Nutzbar vs. hineingeladen.** Die Hauptzahl kommt aus den **Entladefenstern**, also der Energie, die tatsächlich
   herauskommt. Die Ladefenster ergeben die Energie, die hineinmuss. Das Verhältnis beider ist der gemessene
   Wirkungsgrad, ein eigener Gesundheitswert.
8. **Je Einheit.** Viele gestapelte Systeme messen die Leistung nur für alle Einheiten zusammen. Deshalb gilt je
   Fenster E ≈ k_Master·ΔSOC_Master + k_Slave·ΔSOC_Slave (k = Wh je Prozentpunkt). Über viele Fenster wird das robust
   gelöst (Huber-Regression), der Bereich kommt aus dem Bootstrap. Das funktioniert, weil die Einheiten in den
   Fenstern unterschiedlich viel Prozent bewegen. Tun sie das nicht, heißt es „Aufteilung nicht bestimmbar“.
   - Ladefenster gehen mit dem gemessenen Wirkungsgrad umgerechnet ein.
   - Verwendet wird nur der Teil eines Fensters, in dem alle Einheiten Werte liefern.
9. **Ältere Historie.** Vor dem Prometheus-Zeitraum gibt es nur HA-Stundenstatistik (Mittel/Min/Max je Stunde).
   Daraus werden grobe Fenster gebildet:
   - ΔSOC aus den Stundenmitteln
   - Energie aus den Randstunden zur Hälfte plus den vollen Stunden dazwischen
   - deutlich größere Unsicherheit (1,5 Prozentpunkte je Rand, 4 % Energie)

   Sie sind als Quelle „stunden“ gekennzeichnet und werden nur verwendet, wo es keine Minutenwerte gibt.
10. **Vollzyklen-Äquivalente.**
    - System: entladene Energie / Nennkapazität.
    - Je Einheit: Summe der SOC-Abwärtsschritte / 100. Über Schlaf- und Datenlücken hinweg zählt der Abstand der
      gültigen Werte.
11. **Trend und Prognose.** Robuste Gerade (Theil-Sen) über die verlässlichen Monatswerte, Bereich der Steigung per
    Bootstrap. Daraus kommt „80 % voraussichtlich im …“ und dasselbe für die Garantiegrenze.
    - Prognosen gibt es erst ab 6 Monaten mit Schätzung, vorher steht dort „noch zu früh“.
    - Ohne messbaren Verlust steht dort „nicht absehbar“.

Geprüft ist das an synthetischen Speichern mit bekannter Kapazität (siehe [Tests](#tests)). Die Simulation enthält
Wolken, Wirkungsgrade, ganze Prozent, Akkudetails alle 5 min, einen schlafenden Slave, Lücken, Kalibriersprünge und
künstliche Alterung. Ergebnis von `python -m akd synthetic`: Alle Kapazitäten (System und je Einheit) liegen auf
±2,3 % genau, die Wahrheit liegt immer im Bereich; eine Alterung von 8 %/Jahr wird mit −7,9 (wahr −7,8)
Prozentpunkten pro Jahr erkannt.

**Grenzen:**

- **Kapazität je Einheit:** Laden und entladen beide Einheiten im Gleichschritt, lässt sich die gemeinsam gemessene
  Energie kaum aufteilen, und der Akku-Doktor sagt für die einzelnen Einheiten ehrlich „keine verlässliche Aussage“.
  Die Systemkapazität bleibt davon unberührt. Altert eine Einheit schneller, laufen die SOC-Werte auseinander, und
  genau dann wird die Aufteilung wieder bestimmbar.
- Die Genauigkeit hängt am Leistungssensor des Speichers. Eine systematische Abweichung lässt sich ohne externe
  Messung nicht erkennen, deshalb ist sie pauschal mit ±3 % im Bereich enthalten.
- Im Winter gibt es weniger und kleinere Fenster. Dann sagt der Akku-Doktor eher „keine verlässliche Aussage“.

## Master/Slave-Analyse

Täglich werden diese Kennzahlen berechnet:

- mittlere und größte SOC-Differenz (Master − Slave), auch nur bei Sonne
- Minuten mit Drosselung durch eine bestehende SOC-Automation (`throttle_flag`, nur gelesen)
- Aufwachzeit: Master = erste Minute nach 03:00 mit Solarleistung, Slave = erste stabile gültige Minute danach
- Aufwachverzug in Minuten
- Laderate je Einheit (%/h in reinen Lademinuten)

**Umverkabelung („Umstecken“):**

- `master_slave.replug_date: auto` sucht im Suchbereich den Tag, ab dem sich die mittlere Differenz am deutlichsten
  ändert (zwei Mittelwerte, kleinste Fehlerquadrate).
- Dieser Bruch wird mit einem Permutationstest geprüft, der für jede Permutation den besten Bruch neu sucht (sonst wäre
  p geschönt). Ist der Bruch nicht deutlich, gibt es kein Datum und keinen Vergleich, sondern die Bitte, das Datum
  einzutragen.

**Vorher/Nachher:** bis zu 28 Tage je Seite. Je Kennzahl gibt es diese Angaben:

- Differenz der Mittelwerte mit 95-%-Bereich (Bootstrap) und p-Wert (Permutationstest)
- Bewertung „signifikant“, „nicht signifikant“ oder „vorläufig“ (unter 5 Tage auf einer Seite)
- zusätzlich ein saisonbereinigter Wert: Vergleich nur zwischen Tagen mit ähnlichem Solarertrag

**Meldungen:**

- Die Differenz steigt über Wochen: die letzten 14 Tage liegen mindestens 3 Prozentpunkte über den Tagen davor, bei
  p < 0,05.
- Eine Einheit hat 5 % weniger nutzbare Kapazität als die andere, und die Bereiche sind getrennt.

## Garantie

Die Vorlage nutzt die Bedingungen des Growatt NOAH 2000 („NOAH 2000 Warranty Terms and Conditions“):

- 10 Jahre oder 4000 Vollzyklen, was zuerst eintritt.
- Anspruch erst, wenn die Kapazität **unter 50 %** von 2,048 kWh je Akku liegt.
- Grundlage 300 Zyklen pro Jahr, darüber sinkt die Garantie anteilig.
- Laden bei 0–45 °C, Entladen bei −20–45 °C.

`warranty` an den eigenen Hersteller anpassen. Der Akku-Doktor zeigt außerdem die **Referenzkurve des Herstellers**:
`95 % × (1 − 1,5 %·Jahre − 0,011667 %·Zyklen) × (1 − Minderung wegen > 300 Zyklen/Jahr)`.

Der **Garantiewächter** warnt in diesen Fällen (höchstens alle 30 Tage erneut):

- Die Untergrenze des Bereichs liegt unter 55 % (Grenze + 5).
- Die Prognose erreicht 50 % vor Garantieende.
- 12 Monate vor Ablauf, als Erinnerung, ein Dossier zu erstellen.
- 4000 Zyklen oder mehr als 300 Zyklen pro Jahr sind absehbar.

## Selbstüberwachung und Meldungen

Lieber eine Meldung als eine stille Lücke:

| Ereignis | Meldung |
|---|---|
| Entität fehlt | sofort, mit ähnlichen Namen |
| Entität umbenannt (gleiche `unique_id`) | Akku-Doktor liest automatisch unter dem neuen Namen weiter und bittet um Nachtrag in der YAML |
| Integration einer Entität geändert | Hinweis (Werte prüfen) |
| Einheit geändert (z. B. kW statt W) | Werte werden **nicht** übernommen, bis geklärt (danach `python -m akd reset-baseline`) |
| HA-Update | als Ereignis vermerkt |
| über 2 h keine frischen Werte | tagsüber oder solange der SOC über der Entladegrenze liegt |
| SOC steht 3 h still, obwohl ≥ 300 Wh fließen | Akkudetails der Cloud hängen |
| Slave einen ganzen Sonnentag ohne Werte | Hinweis am nächsten Tag |
| Temperatur außerhalb der Garantiegrenzen | **sofort, auch nachts**; ab 40 °C Hinweis |
| Kapazitätseinbruch | letzte 14 Tage ≥ 8 % unter den Tagen davor, Bereiche getrennt; sofort |

Jede Meldung kommt einmal; wenn sie sich erledigt, kommt eine Entwarnung. Nicht dringende Meldungen warten in der
Ruhezeit (22–08 Uhr). Ist HA nicht erreichbar, bleiben Meldungen in der Warteschlange und werden nachgeliefert.

## Daten, Aufbewahrung, Sicherung

- Alles liegt in `data/battery-doctor.sqlite` (Installationen der Version 1.0: `data/akku-doktor.sqlite`), dazu
  Berichte und Dossiers unter `data/`.
- **Minutenwerte** werden 180 Tage gehalten und dann zu **15-Minuten-Werten** (Mittel, Min, Max, erster/letzter Wert)
  verdichtet. Die 15-Minuten-Werte bleiben unbegrenzt.
- **Messfenster, Schätzungen, Tageswerte, Meldungen und Berichte** werden nie gelöscht (wenige MB pro Jahr).
- Nach der Verdichtung kann für Zeiträume älter als 180 Tage nicht mehr neu nach Fenstern gesucht werden; die damals
  gefundenen Fenster und Ergebnisse bleiben vollständig erhalten.
- **Sicherung:** Täglich um 03:20 kommt eine konsistente Kopie der Datenbank nach `/backup` (z. B. eine
  NAS-Freigabe), dazu Berichte und Dossiers; aufbewahrt werden 30 Tage. Die Datenbank wird erst lokal gesichert und
  dann als normale Datei kopiert, weil SQLite direkt auf einer CIFS-Freigabe an Dateisperren hängen bleiben kann.
  Schlägt das fehl, kommt eine Meldung.
- Datenbanken der Version 1.0 (deutsche interne Codes) werden beim Start automatisch migriert.

## Befehle

```bash
docker exec battery-doctor python -m akd status          # Kurzstatus
docker exec battery-doctor python -m akd selftest        # Verbindung, Entitäten, Meldedienst prüfen
docker exec battery-doctor python -m akd analyze         # alles neu auswerten (nach Parameteränderung)
docker exec battery-doctor python -m akd prelearn        # Historie erneut einlesen + neu auswerten
docker exec battery-doctor python -m akd report 2026-11  # Monatsbericht erstellen
docker exec battery-doctor python -m akd dossier system  # Garantie-Dossier (system | <einheit>)
docker exec battery-doctor python -m akd synthetic       # Schätzlogik an synthetischen Speichern (Abnahme-Nachweis)
docker exec battery-doctor python -m akd reset-baseline  # gemerkte Einheiten/Integrationen neu erfassen
```

Die deutschen Befehlsnamen der Version 1.0 (`selbsttest`, `analyse`, `vorlernen`, `bericht`, `synthetik`,
`basis-neu`) funktionieren weiterhin.

Update nach Codeänderung: `docker build --target test . && docker compose up -d --build`.

## Tests

`docker build --target test .` führt alle Tests aus (lokal: `python -m pytest -q tests/`). Was geprüft wird, steht
ausführlich im englischen README im Abschnitt [Tests](README.md#tests): synthetische Ladekurven mit bekannter
Kapazität (inkl. künstlicher Alterung, dünner und grober Daten), Master/Slave mit und ohne echte Umverkabelung, ein
End-to-End-Test gegen ein nachgebautes Home Assistant und Prometheus inklusive Nur-lesen-Garantie, sowie die
Kompatibilität mit deutschen Installationen der Version 1.0.

## Fehlerbehebung

- **Weboberfläche zeigt „erste Auswertung läuft“:** Beim ersten Start einige Minuten warten, `docker logs battery-doctor`.
- **Alle Kapazitäten „unknown“:** Grund steht im Sensor-Attribut `grund` und auf der Übersicht. Im Winter oder nach
  Datenlücken gibt es oft zu wenige saubere Fenster. Das ist Absicht, kein Fehler.
- **„token rejected“:** siehe [Secrets](#secrets-token-eintragen-und-wechseln).
- **Sicherung schlägt fehl:** Freigabe eingehängt? Schreibrechte für `AKD_UID`?
- **Entität umbenannt:** Der Akku-Doktor folgt automatisch. Den neuen Namen in `config.yaml` eintragen, damit es
  dauerhaft stimmt.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
