package main

import (
	"database/sql"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type retrievalSettings struct {
	PrefetchFloor      int     `json:"prefetch_floor"`
	PrefetchMultiplier int     `json:"prefetch_multiplier"`
	RRFK               int     `json:"rrf_k"`
	VectorWeight       float64 `json:"vector_weight"`
	KeywordWeight      float64 `json:"keyword_weight"`
	MMRLambda          float64 `json:"mmr_lambda"`
}

func defaultRetrievalSettings() retrievalSettings {
	boundedInt := func(name string, fallback, maximum int) int {
		value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
		if err == nil && value >= 1 && value <= maximum {
			return value
		}
		return fallback
	}
	boundedFloat := func(name string, fallback float64) float64 {
		value, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(name)), 64)
		if err == nil && value >= 0 && value <= 1 {
			return value
		}
		return fallback
	}
	settings := retrievalSettings{
		PrefetchFloor:      boundedInt("RETRIEVAL_PREFETCH_FLOOR", 50, 1000),
		PrefetchMultiplier: boundedInt("RETRIEVAL_PREFETCH_MULTIPLIER", 5, 20),
		RRFK:               boundedInt("RETRIEVAL_RRF_K", 60, 1000),
		VectorWeight:       boundedFloat("RETRIEVAL_VECTOR_WEIGHT", 0.7),
		KeywordWeight:      boundedFloat("RETRIEVAL_KEYWORD_WEIGHT", 0.3),
		MMRLambda:          boundedFloat("RETRIEVAL_MMR_LAMBDA", 0.7),
	}
	if settings.VectorWeight+settings.KeywordWeight == 0 {
		settings.VectorWeight, settings.KeywordWeight = 0.7, 0.3
	}
	return settings
}

func readRetrievalSettings(input io.Reader) (retrievalSettings, bool) {
	decoder := json.NewDecoder(input)
	decoder.DisallowUnknownFields()
	var raw struct {
		Floor      *int     `json:"prefetch_floor"`
		Multiplier *int     `json:"prefetch_multiplier"`
		RRFK       *int     `json:"rrf_k"`
		Vector     *float64 `json:"vector_weight"`
		Keyword    *float64 `json:"keyword_weight"`
		Lambda     *float64 `json:"mmr_lambda"`
	}
	if decoder.Decode(&raw) != nil || decoder.Decode(new(json.RawMessage)) != io.EOF ||
		raw.Floor == nil || raw.Multiplier == nil || raw.RRFK == nil ||
		raw.Vector == nil || raw.Keyword == nil || raw.Lambda == nil {
		return retrievalSettings{}, false
	}
	settings := retrievalSettings{*raw.Floor, *raw.Multiplier, *raw.RRFK, *raw.Vector, *raw.Keyword, *raw.Lambda}
	valid := settings.PrefetchFloor >= 1 && settings.PrefetchFloor <= 1000 &&
		settings.PrefetchMultiplier >= 1 && settings.PrefetchMultiplier <= 20 &&
		settings.RRFK >= 1 && settings.RRFK <= 1000 &&
		settings.VectorWeight >= 0 && settings.VectorWeight <= 1 &&
		settings.KeywordWeight >= 0 && settings.KeywordWeight <= 1 &&
		settings.MMRLambda >= 0 && settings.MMRLambda <= 1 &&
		settings.VectorWeight+settings.KeywordWeight > 0
	return settings, valid
}

func publicRetrievalSettings(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !knowledgeAdmin(w, r, conn) {
			return
		}
		target := strings.TrimSpace(os.Getenv("RETRIEVAL_SETTINGS_PATH"))
		if target == "" {
			target = "../backend/retrieval_settings.json"
		}
		if r.Method == http.MethodGet {
			settings := defaultRetrievalSettings()
			if file, err := os.Open(target); err == nil {
				defer file.Close()
				if saved, valid := readRetrievalSettings(io.LimitReader(file, 1<<16)); valid {
					settings = saved
				}
			}
			writeJSON(w, http.StatusOK, settings)
			return
		}
		settings, valid := readRetrievalSettings(http.MaxBytesReader(w, r.Body, 1<<16))
		if !valid {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "检索设置参数不合法"})
			return
		}
		if err := saveRetrievalSettings(target, settings); err != nil {
			slog.Error("检索设置保存失败", "err", err)
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"detail": "检索设置保存失败，请检查配置目录写权限或稍后重试"})
			return
		}
		writeJSON(w, http.StatusOK, settings)
	}
}

func saveRetrievalSettings(target string, settings retrievalSettings) error {
	file, err := os.CreateTemp(filepath.Dir(target), filepath.Base(target)+".*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	defer file.Close()
	if err := file.Chmod(0644); err != nil {
		return err
	}
	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(settings); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), target)
}
