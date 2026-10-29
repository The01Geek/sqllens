Enforce bounded ASCII profile names across administration, persistence and
query selection, with short errors that do not echo rejected names. Invalid
legacy names require manual repair; profile writes stay disabled until
repair and restart so excluded entries are not lost. (#211, #214)
