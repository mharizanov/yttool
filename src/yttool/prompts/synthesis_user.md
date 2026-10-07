Synthesize these {video_count} YouTube video summaries about the research topic: {query}

Video roster:

{roster_json}

Per-video summaries (source IDs are prefixed V01., V02., ... per video):

{summaries_json}

Community insights distilled from each video's comments (like_count is the viewers' vote; may be empty):

{community_json}

Quote pool - transcript-verified quotable lines from each video (reference by id only; may be empty):

{quotes_json}

Return only valid JSON using the required schema from the system prompt.
