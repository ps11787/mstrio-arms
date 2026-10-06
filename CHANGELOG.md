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
- Neue Einträge in `arms.t_mig_dim_migration` erhalten jetzt eine numerische `migration_id` als `MAX(migration_id) + 1`.
- Der Import liest die zu verarbeitenden Requests direkt aus `arms.t_mig_dim_request` anhand der konfigurierten Statusliste und ergänzt fehlende Kontextwerte bei Bedarf aus dem Request-Report.
- Der Import protokolliert aktive Migrationsjobs, schreibt Erfolgs- und Fehlerkommentare in ARMS und trennt die Aufloesung von Zielumgebung, Zielprojekt und Zielstatus klarer in eigene Hilfsfunktionen auf.

### Verifiziert

- Ausgeführt: `pytest tests/test_create_package_dependency_expansion.py tests/test_import_package_lock_retry.py -q`
- Ergebnis: `22 passed in 2.12s`
