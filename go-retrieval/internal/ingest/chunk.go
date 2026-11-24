package ingest

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// Job 是一次入库预处理任务：把原始文档切成可检索的分块。
type Job struct {
	ID      string `json:"id"`
	Title   string `json:"title"`
	Content string `json:"content"`
}

// Chunk 是切分后的一个片段。
type Chunk struct {
	Index int    `json:"index"`
	Text  string `json:"text"`
}

// Result 是任务结果。
type Result struct {
	ID          string  `json:"id"`
	Chunks      []Chunk `json:"chunks"`
	Fingerprint string  `json:"fingerprint"`
	Err         error   `json:"-"`
}

// DefaultMaxChunkRunes 是单块的目标字符数。
//
// 按「字符」而非字节切分：中文一个字符三字节，按字节切会切坏多字节字符。
const DefaultMaxChunkRunes = 500

// Fingerprint 计算标题与正文的规范化指纹，用于识别重复入库。
func Fingerprint(title, content string) string {
	normalized := strings.Join(strings.Fields(title+"\n"+content), " ")
	sum := sha256.Sum256([]byte(normalized))
	return hex.EncodeToString(sum[:])
}

// Split 把正文按段落聚合切块，尽量不打断段落。
//
// 策略：先按空行分段，再贪心地把段落拼成不超过 maxRunes 的块；
// 单段本身就超长时按字符硬切（保证不丢内容）。
func Split(content string, maxRunes int) []Chunk {
	if maxRunes <= 0 {
		maxRunes = DefaultMaxChunkRunes
	}

	paragraphs := splitParagraphs(content)
	if len(paragraphs) == 0 {
		return nil
	}

	var chunks []Chunk
	var buf strings.Builder
	bufRunes := 0

	flush := func() {
		if bufRunes == 0 {
			return
		}
		chunks = append(chunks, Chunk{Index: len(chunks), Text: strings.TrimSpace(buf.String())})
		buf.Reset()
		bufRunes = 0
	}

	for _, para := range paragraphs {
		runes := []rune(para)

		// 单段超长：先冲刷已有缓冲，再按字符硬切
		if len(runes) > maxRunes {
			flush()
			for start := 0; start < len(runes); start += maxRunes {
				end := start + maxRunes
				if end > len(runes) {
					end = len(runes)
				}
				chunks = append(chunks, Chunk{Index: len(chunks), Text: strings.TrimSpace(string(runes[start:end]))})
			}
			continue
		}

		// 拼上这一段会超限：先冲刷，再开始新块
		if bufRunes+len(runes) > maxRunes && bufRunes > 0 {
			flush()
		}
		if bufRunes > 0 {
			buf.WriteString("\n\n")
			bufRunes += 2
		}
		buf.WriteString(para)
		bufRunes += len(runes)
	}
	flush()

	return chunks
}

// splitParagraphs 按空行分段并去掉空白段。
func splitParagraphs(content string) []string {
	raw := strings.Split(strings.ReplaceAll(content, "\r\n", "\n"), "\n\n")
	out := make([]string, 0, len(raw))
	for _, p := range raw {
		if trimmed := strings.TrimSpace(p); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}
