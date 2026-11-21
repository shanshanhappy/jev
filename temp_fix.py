def _check_sentence_negation(text: str, start: int, end: int) -> bool:
    """
    检查匹配词所在句子的否定词
    
    句子以句号、问号、感叹号、分号、换行符分隔。
    如果整个句子有否定词，即使不在局部窗口内，也认为有否定。
    
    注意：转折词可以作为否定边界，分隔前后两个部分。
    """
    # 找到匹配词所在的句子
    sentence_start = 0
    for delim in SENTENCE_DELIMITERS:
        pos = text.rfind(delim, 0, start)
        if pos > sentence_start:
            sentence_start = pos + 1
    
    sentence_end = len(text)
    for delim in SENTENCE_DELIMITERS:
        pos = text.find(delim, end)
        if pos != -1 and pos < sentence_end:
            sentence_end = pos
    
    sentence = text[sentence_start:sentence_end]
    match_pos_in_sentence = start - sentence_start
    
    # 检查整个句子是否有否定词
    for neg_word in NEGATION_WORDS:
        if neg_word in sentence:
            neg_pos = sentence.find(neg_word)
            
            # 如果否定词在匹配词之前，检查是否有转折词分隔
            if neg_pos < match_pos_in_sentence:
                between_text = sentence[neg_pos + len(neg_word):match_pos_in_sentence]
                has_contrast = any(cw in between_text for cw in CONTRAST_WORDS)
                
                # 如果没有转折词分隔，否定影响匹配
                if not has_contrast:
                    return True
    
    return False
