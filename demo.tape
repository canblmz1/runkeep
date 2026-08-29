# runkeep demo — recorded live against GitHub with `vhs demo.tape`.
# GH_TOKEN is read from the environment; it is never typed on screen or stored in this file.

Output demo.gif

Set Shell bash
Set FontSize 14
Set Width 1100
Set Height 640
Set Padding 18
Set TypingSpeed 22ms
Set PlaybackSpeed 1.5

Hide
Type "cd $(mktemp -d) && clear"
Enter
Show
Sleep 400ms

Type "# how much CI history is pallets/click about to lose?"
Enter
Sleep 300ms
Type "runkeep check pallets/click"
Enter
Wait+Screen@20s /retention window/
Sleep 1200ms

Type "# archive its recent history into a local SQLite file"
Enter
Sleep 300ms
Type "runkeep rescue pallets/click --since 2026-08-27 --db click.db"
Enter
Wait+Screen@45s /Core CI history/
Sleep 1200ms

Type "# prove the archive matches GitHub via an independent re-query"
Enter
Sleep 300ms
Type "runkeep verify click.db pallets/click --sample 3"
Enter
Wait+Screen@45s /\ds\)/
Sleep 2000ms
