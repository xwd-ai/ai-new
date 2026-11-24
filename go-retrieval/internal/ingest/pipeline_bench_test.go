package ingest

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func benchPipelineConfig() PipelineConfig {
	return PipelineConfig{Workers: 4, QueueSize: 64}
}

func benchJob(seq int64) Job {
	return Job{
		ID:      fmt.Sprintf("bench-%d", seq),
		Title:   "基准标题",
		Content: strings.Repeat("并发分块与指纹计算", 50),
	}
}

// BenchmarkPipelineSubmit 用 RunParallel 压 Submit 的并发路径。
// 每个任务内容唯一，避免命中同指纹缓存，测的是完整链路。
func BenchmarkPipelineSubmit(b *testing.B) {
	p := NewPipeline(benchPipelineConfig())
	defer p.Close()

	var seq atomic.Int64
	b.ReportAllocs()
	b.ResetTimer()

	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			job := benchJob(seq.Add(1))
			if _, err := p.Submit(context.Background(), job); err != nil {
				b.Errorf("提交失败: %v", err)
				return
			}
		}
	})
}

// BenchmarkPipelineBatched 模拟 HTTP handler 的批量入库：每轮并发提交一批再汇合。
func BenchmarkPipelineBatched(b *testing.B) {
	const batch = 16

	p := NewPipeline(benchPipelineConfig())
	defer p.Close()

	var seq atomic.Int64
	b.ReportAllocs()
	b.ResetTimer()

	for i := 0; i < b.N; i++ {
		var wg sync.WaitGroup
		wg.Add(batch)
		for j := 0; j < batch; j++ {
			go func() {
				defer wg.Done()
				job := benchJob(seq.Add(1))
				if _, err := p.Submit(context.Background(), job); err != nil {
					b.Errorf("批量提交失败: %v", err)
				}
			}()
		}
		wg.Wait()
	}
}
