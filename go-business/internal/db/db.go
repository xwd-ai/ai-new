// Package db 提供数据库连接。
//
// 迁移第一阶段只支持 SQLite，且以**只读模式**打开：Go 侧此刻只承担查询，
// 从连接层面禁止写入，避免出现「Python 和 Go 同时写库」的隐患。
// 后续迁移工单写操作时，再单独提供一个读写连接并由写入方显式申请。
package db

import (
	"database/sql"
	"fmt"
	"os"

	_ "modernc.org/sqlite" // 纯 Go SQLite 驱动，无需 cgo
)

// Open 打开只读连接。路径优先取 DB_PATH，缺省用仓库内开发库。
func Open() (*sql.DB, error) {
	path := os.Getenv("DB_PATH")
	if path == "" {
		path = "../backend/app.db"
	}

	// mode=ro：物理只读，业务代码即使写错 SQL 也无法改数据
	dsn := fmt.Sprintf("file:%s?mode=ro", path)
	conn, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("打开数据库失败: %w", err)
	}
	if err := conn.Ping(); err != nil {
		conn.Close()
		return nil, fmt.Errorf("连接数据库失败: %w", err)
	}
	return conn, nil
}
