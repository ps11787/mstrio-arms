# Changelog

## Unreleased

### Create Package Workflow

- Die Erstellung eines Migration Packages bricht nicht mehr ab, wenn `Migration Obj@GUID` leer ist oder das Migrationsobjekt auf dem Server nicht mehr vorhanden ist.
- Ein bestehendes Migrationsobjekt wird nur dann gelöscht, wenn es erfolgreich geladen werden kann. Andernfalls wird ein neues Package erstellt, ohne das alte Objekt zu zerstören.
- Die Abhängigkeitsauflösung für Shortcut-Ziele wurde erweitert: Rekursiv werden alle relevanten Abhängigkeiten wie Metriken, Datasets, Reports, Filter und Prompts berücksichtigt.
- MicroStrategy-Systemobjekte wie `>`, `*` und `DSS Built-in Package` werden aus der Package-Inhaltsliste herausgefiltert, damit keine synthetischen Objekte als echte Abhängigkeiten aufgenommen werden.
- Shortcut-Ziele werden nun als echte Package-Root-Objekte aufgenommen. Statt des Shortcut-Containers landen jetzt die aufgelösten Zielobjekte, einschließlich Document-Objekten, direkt im Package-Inhalt.
- Die Validation-Loop verarbeitet Shortcut-Ziele gegen die Quellprojekt-Verbindung, damit Document- und Report-Ziele zuverlässig gefunden und dem Package hinzugefügt werden.
- Objekt-Typen werden für die ARMS-Objekttabelle als kanonische Typnamen gespeichert, und SQL-Werte wie Objektnamen mit Apostrophen werden sicher escaped.

### Import Package Workflow

- Der Import löst Zielprojekte nun stabil über ARMS-Metadaten auf: bevorzugt über `tgt_project_id`, danach über Status, Zielumgebung und Präfix statt über unsichere Fallback-Namen.
- Für `arms.t_mig_dim_migration.project_id` wird jetzt gezielt die numerische Zielprojekt-ID verwendet: zuerst `tgt_project_id`/`target_project_id` aus dem Request, danach Fallback über die Zielprojekt-Namensauflösung in `arms.t_mig_lu_project`.
- Neue Einträge in `arms.t_mig_dim_migration` erhalten jetzt eine numerische `migration_id` als `MAX(migration_id) + 1`.
- Der Import liest die zu verarbeitenden Requests direkt aus `arms.t_mig_dim_request` anhand der konfigurierten Statusliste und ergänzt fehlende Kontextwerte bei Bedarf aus dem Request-Report.
- Der Import protokolliert aktive Migrationsjobs, schreibt Erfolgs- und Fehlerkommentare in ARMS und trennt die Aufloesung von Zielumgebung, Zielprojekt und Zielstatus klarer in eigene Hilfsfunktionen auf.
- Wenn ein bestehendes Migrationsobjekt beim Start mit `packageInfo.status=locked` blockiert, nutzt der Import einen robusten Fallback: statusbasierten Importstart ohne Rebinding und bei Bedarf Klonen eines neuen Migrationsobjekts aus dem bestehenden Package.
- Die Post-Import-Prüfung wurde erweitert: Erfolgsfreigabe erfolgt auf Basis von Import-Status und Undo-Package-Verfügbarkeit; optional kann ein entsperrtes Package weiterhin erzwungen werden (`MSTR_REQUIRE_UNLOCKED_PACKAGE_AFTER_IMPORT`).
- Nach erfolgreichem Import wird `undoRequestStatus` nicht mehr dauerhaft auf `PENDING` belassen: der Workflow normalisiert auf `REQUESTED` (konfigurierbar über `MSTR_REQUIRE_UNDO_REQUESTED_AFTER_IMPORT`) und bricht bei Fehlschlag in diesem Schritt kontrolliert ab.

### Verifiziert

- Ausgeführt: `pytest tests/test_import_package_lock_retry.py -q`
- Ergebnis: `16 passed in 2.53s`
