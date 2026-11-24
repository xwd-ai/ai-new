// Package db 提供数据库连接。
//
// 分两个入口，按最小权限原则隔离读写能力：
//
//   - Open：只读连接（mode=ro），供订单、工单列表、令牌校验等查询使用。
//     物理只读意味着业务代码即使写错 SQL 也改不了数据。
//   - OpenWrite：读写连接（mode=rw），只给工单状态流转这类确实要写的路径。
//
// 两条连接指向同一个 SQLite 文件。SQLite 是单写者模型，跨进程写入靠文件锁
// 串行化，因此写连接必须设置 busy_timeout，否则 Python 与 Go 同时写时
// 会直接抛出 "database is locked"。
package db

import (
	"database/sql"
	"fmt"
	"os"

	_ "modernc.org/sqlite" // 纯 Go SQLite 驱动，无需 cgo
)

// defaultPath 是开发库位置：容器里通过 DB_PATH 覆盖。
const defaultPath = "../backend/app.db"

func databasePath() string {
	if path := os.Getenv("DB_PATH"); path != "" {
		return path
	}
	return defaultPath
}

// Open 打开只读连接。路径优先取 DB_PATH，缺省用仓库内开发库。
func Open() (*sql.DB, error) {
	// mode=ro：物理只读，业务代码即使写错 SQL 也无法改数据
	dsn := fmt.Sprintf("file:%s?mode=ro", databasePath())
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

// OpenWrite 打开读写连接，供工单状态流转使用。
//
// 三个关键参数：
//
//   - mode=rw：库文件不存在时直接报错，而不是默默建一个空库——
//     「连到空库」写出的工单谁也读不到，比启动失败更难排查。
//   - _pragma=busy_timeout(5000)：拿不到文件锁时最多等 5 秒。
//     Python 与 Go 可能同时写，没有这个等待，用户会遇到随机的写入失败。
//   - _txlock=immediate：BeginTx 直接取写锁，避免 SQLite 经典的
//     「读事务升级写锁」失败（DEFERRED 会先拿共享锁，升级时可能被拒）。
//
// 连接池限制为 1：SQLite 单写者，多个连接同时写只会互相等锁。
// 工单流转是低频操作，串行化的代价远小于自锁风险。
func OpenWrite() (*sql.DB, error) {
	path := databasePath()
	if _, err := os.Stat(path); err != nil {
		return nil, fmt.Errorf("数据库文件不存在: %w", err)
	}

	dsn := fmt.Sprintf("file:%s?mode=rw&_pragma=busy_timeout(5000)&_txlock=immediate", path)
	conn, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("打开数据库失败: %w", err)
	}
	conn.SetMaxOpenConns(1)
	if err := conn.Ping(); err != nil {
		conn.Close()
		return nil, fmt.Errorf("连接数据库失败: %w", err)
	}
	return conn, nil
}
