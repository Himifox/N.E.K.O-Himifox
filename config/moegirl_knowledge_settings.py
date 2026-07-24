"""Safe defaults for the local public meme knowledge runtime."""

# CHIME is shipped as a fixed, MIT-licensed JSON asset in the application
# package. Enabling it never creates a CHIME network request.
CHIME_KNOWLEDGE_ENABLED = True
MOEGIRL_KNOWLEDGE_AUTO_CONTEXT_ENABLED = True
MOEGIRL_KNOWLEDGE_AUTO_CONTEXT_MAX_HITS = 1
