# ai-queryBoat
Built a conversational assistant with multi-turn memory, where the LLM autonomously decides which tools to call (e.g. weather, restaurant lookup) in a multi-step reasoning loop. Implemented the agent loop (call tool, observe result, decide next step) with a max-step guard and graceful handling of tool failures.
