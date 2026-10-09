# One-click PaaS hosting of the public try-in-browser demo (form at /, conversation at /chat).
# Hosts that read a Procfile (Render, Railway, Koyeb, Heroku-likes) inject $PORT; serve --public
# binds 0.0.0.0, serves live surveys only, and sends no passbacks unless SURVEY_INTEGRATION is set.
# Storage is SURVEY_STORE (default: file under ./survey_data — ephemeral on most PaaS disks; set a
# database or mount a persistent disk before fielding real respondents).
# Build step: pip install ".[llm]"   (Claude, GPT or OpenAI-compatible; omit extras for the offline stub)
web: survey-agent serve --public
