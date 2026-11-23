package search

import (
	"strings"
	"unicode"
)

// tokenize 切分词元：中文按单字、英文与数字按连续串，其余字符作为分隔符。
//
// 中文单字区分度很低（「的」「天」几乎每篇都有），所以正文匹配会再过滤掉
// 单字词元（见 documentTokens），标题与关键词则保留。
func tokenize(text string) []string {
	var tokens []string
	var buf []rune

	flush := func() {
		if len(buf) > 0 {
			tokens = append(tokens, strings.ToLower(string(buf)))
			buf = buf[:0]
		}
	}

	for _, r := range text {
		switch {
		case unicode.Is(unicode.Han, r):
			// 中文按单字切分
			flush()
			tokens = append(tokens, string(r))
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			buf = append(buf, r)
		default:
			flush()
		}
	}
	flush()
	return tokens
}

// isSingleHan 判断词元是否为单个汉字。
func isSingleHan(token string) bool {
	rs := []rune(token)
	return len(rs) == 1 && unicode.Is(unicode.Han, rs[0])
}

// documentTokens 组装文档词元：正文过滤单字，标题与关键词各计两次以提升权重。
func documentTokens(doc Document) []string {
	tokens := make([]string, 0, 64)

	for _, token := range tokenize(doc.Content) {
		if isSingleHan(token) {
			continue
		}
		tokens = append(tokens, token)
	}

	// 标题与关键词是人工标注的强信号，重复计入提高权重
	titleTokens := tokenize(doc.Title)
	for _, kw := range doc.Keywords {
		tokens = append(tokens, tokenize(kw)...)
	}
	tokens = append(tokens, titleTokens...)
	tokens = append(tokens, titleTokens...)
	return tokens
}
