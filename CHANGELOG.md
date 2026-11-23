# Changelog

## 1.1.0 (2026-11-22)

- **Star rewards:** chores earn stars (none, 1, 2, 3 or 5). Kids spend them on rewards you set up, with ideas to start from. Parents can give or take bonus stars. Spending and editing rewards need the Settings PIN, if one is set.
- **Snap a flyer:** turn a photo, PDF or pasted email into events with Claude, review and edit them, then add them in one tap.
- **Day view:** one column per person for a single day, plus tonight's dinner. It can also be chosen as the start screen.
- Settings: the Claude section now covers both meal ideas and importing.

## 1.0.0 (2026-11-22)

First public release.

- Week, month and agenda calendar views with a color for each person
- Linked Google, iCloud and Outlook calendars (read-only ICS links), with a refresh button and an adjustable check interval
- Events added on the screen or from any phone on the home Wi-Fi
- Chore chart with repeating chores and daily check-offs
- Shared lists (groceries, to-dos, anything else)
- Weekly meal planner; tonight's dinner shows on the calendar
- Pantry list with optional recipe search (Spoonacular) and optional Claude meal ideas
- First-run setup screens and an in-app Settings page; no config files
- Optional settings PIN
- MariaDB/MySQL storage (creates its own database), or plain JSON files
- Keeps working from its last copy if the server or database goes down
- Weather, night mode, text size, auto-return to today
- Multi-arch Docker image (amd64 + arm64) and an Unraid template
