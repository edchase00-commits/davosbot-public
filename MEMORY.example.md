# Memory

## Identity
- Full name: [Your name]
- Your number: [Your phone number, e.g. <phone>]
- From [Your hometown / home state]
- Lives in [Your city]
- Has lived in [City 1], [City 2], now [Current city]

## Communication style (treat as ground truth)
- [Describe your preferred tone, e.g. direct / casual / verbose]
- [Slang or shorthand you use, e.g. "lowkey", "fr", "no cap"]
- [Formatting preferences, e.g. prefers bullet summaries, hates long paragraphs]
- Prefers short responses across all personas

## Sports fandom
- [Favorite college team]
- [Favorite pro sports teams]
- [Any particular sports interests, e.g. UFC, fantasy leagues]

## Family
- Lives in [Family's city/state]
- Sibling: [Name]
- Mom: [Name]
- Dad: [Name]

## Friends / social
- Close friends: [Friend 1 name], [Friend 2 name]
- Core group: [Group nickname if any]
- Weekend energy: [How you spend weekends]

## Fitness
- Goal: [e.g. 5 gym days + 2 walking days]
- Likes [workout styles]
- Current PRs: [lift name and weight, e.g. Bench 225x5]

## Kitchen / food
- [Dietary preferences or cooking style]
- [Favorite foods]

## Side projects
- [Project 1 name] — [one-line description]
- [Project 2 name] — [one-line description]
- DavosBot (this bot)

## Gaming
- [Current game you're playing]
- [Gaming goals or rules you follow]

## Interests
- [Hobby 1]
- [Hobby 2]
- [Hobby 3]

## Shopping profile
- [Price sensitivity, brand preferences, etc.]

## Tech stack
- Bot runs on [local model] via Ollama (local, on the Mac Mini M4) with Gemini as fallback when Ollama is down. All tool-use calls go to Gemini.

## Known people
- [Friend handle]: [<phone>] — admin, dogfooding partner
- [Friend 2 name] — close friend

## Bot capabilities (for self-awareness when asked)
- Can batch-log multiple change requests from a numbered list — calls log_change_request once per item
- "log remove [id]" removes a completed item from the change log
- "no web search" in a message skips Tavily — works for owner and friends
- Approved friends in group chats get web_search tool (5 searches/day) and image analysis (5/day)
- Owner has full tool access in DMs and group chats
