# runkeep demo — recorded live against GitHub with `vhs demo.tape`.
# GH_TOKEN is read from the environment; it is never typed on screen or stored in this file.

Output demo.gif

Set Shell bash
Set FontSize 14
Set Width 1100
Set Height 620
Set Padding 18
Set TypingSpeed 28ms
Set PlaybackSpeed 2.0

Hide
Type "cd $(mktemp -d) && clear"
Enter
Show
Sleep 500ms

Type "# how much CI history is pallets/click about to lose?"
Enter
Sleep 400ms
Type "runkeep check pallets/click"
Enter
Sleep 8s

Type "# archive its recent history into a local SQLite file"
Enter
Sleep 400ms
Type "runkeep rescue pallets/click --since 2026-08-27 --db click.db"
Enter
Sleep 16s

Type "# prove the archive matches GitHub, via an independent re-query"
Enter
Sleep 400ms
Type "runkeep verify click.db pallets/click --sample 3"
Enter
Sleep 9s
