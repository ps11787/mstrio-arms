# Changelog

## Unreleased

### Behoben

- Die Erstellung eines Migration Packages bricht nicht mehr ab, wenn `Migration Obj@GUID` leer ist oder das Migrationsobjekt auf dem Server nicht mehr vorhanden ist.
- Ein bestehendes Migrationsobjekt wird nur dann gelöscht, wenn es erfolgreich geladen werden kann. Andernfalls wird ein neues Package erstellt, ohne das alte Objekt zu zerstören.
- Die Abhängigkeitsauflösung für Shortcut-Ziele wurde erweitert: Rekursiv werden alle relevanten Abhängigkeiten wie Metriken, Datasets, Reports, Filter und Prompts berücksichtigt.
- MicroStrategy-Systemobjekte wie `>`, `*` und `DSS Built-in Package` werden aus der Package-Inhaltsliste herausgefiltert, damit keine synthetischen Objekte als echte Abhängigkeiten aufgenommen werden.

### Verifiziert

- Ausgeführt: `pytest tests/test_create_package_dependency_expansion.py tests/test_import_package_lock_retry.py -q`
- Ergebnis: `10 passed in 2.09s`
