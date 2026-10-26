def browser_input_interrupted(error: Exception) -> bool:
    message = str(error).casefold()
    return any(reason in message for reason in ("browser use is paused", "browser use was paused", "browser use was interrupted"))
