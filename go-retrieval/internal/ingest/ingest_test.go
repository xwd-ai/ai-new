package ingest

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestSplit(t *testing.T) {
	tests := []struct {
		name      string
		content   string
		maxRunes  int
		wantCount int
	}{
		{name: "单段", content: "第一段内容", maxRunes: 100, wantCount: 1},
		{name: "两段可合并", content: "第一段\n\n第二段", maxRunes: 100, wantCount: 1},
		{name: "两段超限分开", content: "第一段\n\n第二段", maxRunes: 5, wantCount: 2},
		{name: "超长段硬切", content: strings.Repeat("字", 25), maxRunes: 10, wantCount: 3},
		{name: "空内容", content: "", maxRunes: 100, wantCount: 0},
		{name: "只有空白", content: "\n\n  \n\n", maxRunes: 100, wantCount: 0},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Split(tt.content, tt.maxRunes)
			if len(got) != tt.wantCount {
				t.Fatalf("分块数期望 %d，实际 %d（%v）", tt.wantCount, len(got), got)
			}
			// 索引必须连续，且每块不超过上限（硬切也遵守）
			for i, c := range got {
				if c.Index != i {
					t.Fatalf("第 %d 块索引为 %d", i, c.Index)
				}
				if n := len([]rune(c.Text)); n > tt.maxRunes {
					t.Fatalf("第 %d 块长度 %d 超过上限 %d", i, n, tt.maxRunes)
				}
			}
		})
	}
}

func TestFingerprint(t *testing.T) {
	base := Fingerprint("标题", "正文内容")

	// 空白差异不应改变指纹（规范化后比较）
	spaced := Fingerprint("标题  ", "\n 正文内容\n")
	if base != spaced {
		t.Fatalf("空白差异应得到相同指纹：%s != %s", base, spaced)
	}

	// 内容不同则指纹不同
	if other := Fingerprint("标题", "另一段正文"); base == other {
		t.Fatal("不同内容不应得到相同指纹")
	}
}

func TestWorkerConcurrent(t *testing.T) {
	const jobs = 24
	w := New(4, 8)
	defer w.Close()

	var wg sync.WaitGroup
	var mu sync.Mutex
	got := make(map[string]int)

	for i := 0; i < jobs; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			res, err := w.Submit(context.Background(), Job{
				ID:      "doc-" + string(rune('a'+i%26)),
				Title:   "标题",
				Content: strings.Repeat("内容", 50),
			})
			if err != nil {
				t.Errorf("任务 %d 失败: %v", i, err)
				return
			}
			mu.Lock()
			got[res.ID]++
			mu.Unlock()
			if len(res.Chunks) == 0 || res.Fingerprint == "" {
				t.Errorf("任务 %d 结果不完整: %+v", i, res)
			}
		}(i)
	}
	wg.Wait()

	if len(got) == 0 {
		t.Fatal("没有任何任务成功")
	}
}

func TestWorkerCloseRejectsNewJobs(t *testing.T) {
	w := New(2, 4)
	w.Close()

	_, err := w.Submit(context.Background(), Job{ID: "after-close", Content: "内容"})
	if !errors.Is(err, ErrClosed) {
		t.Fatalf("关闭后期望 ErrClosed，实际 %v", err)
	}

	// 重复关闭应安全
	w.Close()
}

func TestWorkerCloseWaitsInFlight(t *testing.T) {
	w := New(2, 4)

	done := make(chan struct{})
	go func() {
		defer close(done)
		// 关闭过程中允许返回 ErrClosed，这里只关心协程能正常退出
		_, _ = w.Submit(context.Background(), Job{ID: "inflight", Content: strings.Repeat("字", 3000)})
	}()

	// 让 Submit 有机会把任务放进队列
	time.Sleep(10 * time.Millisecond)
	w.Close()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Close 未等待在途任务完成")
	}
}
