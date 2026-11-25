package main

import (
	"net/http"
	"path/filepath"
)

func registerFrontendRoutes(mux *http.ServeMux, dir string) {
	if dir == "" {
		return
	}
	pages := map[string]string{
		"/{$}": "index.html", "/login": "login.html", "/admin": "admin.html",
		"/settings": "settings.html", "/knowledge": "knowledge.html",
	}
	for path, file := range pages {
		mux.HandleFunc("GET "+path, func(w http.ResponseWriter, r *http.Request) {
			http.ServeFile(w, r, filepath.Join(dir, file))
		})
	}
	mux.Handle("GET /assets/", http.StripPrefix("/assets/", http.FileServer(http.Dir(dir))))
}
