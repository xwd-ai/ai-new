package db

import (
	"context"
	"database/sql"
	"errors"
	"net"
	"os"
	"time"

	"github.com/go-sql-driver/mysql"
)

func mysqlAddress() string {
	host := os.Getenv("MYSQL_HOST")
	if host == "" {
		host = "127.0.0.1"
	}
	port := os.Getenv("MYSQL_PORT")
	if port == "" {
		port = "3307"
	}
	return net.JoinHostPort(host, port)
}

func openMySQL() (*sql.DB, error) {
	dsn := os.Getenv("MYSQL_DSN")
	if dsn == "" {
		config := mysql.NewConfig()
		config.Net, config.Addr = "tcp", mysqlAddress()
		config.User, config.Passwd, config.DBName = os.Getenv("MYSQL_USER"), os.Getenv("MYSQL_PASSWORD"), os.Getenv("MYSQL_DATABASE")
		if _, set := os.LookupEnv("MYSQL_USER"); !set {
			config.User = "ainew"
		}
		if _, set := os.LookupEnv("MYSQL_PASSWORD"); !set {
			config.Passwd = "ainew"
		}
		if _, set := os.LookupEnv("MYSQL_DATABASE"); !set {
			config.DBName = "ainew"
		}
		config.Collation = "utf8mb4_0900_bin"
		config.Timeout = 5 * time.Second
		dsn = config.FormatDSN()
	}
	conn, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, err
	}
	conn.SetMaxOpenConns(16)
	conn.SetMaxIdleConns(4)
	conn.SetConnMaxLifetime(5 * time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := conn.PingContext(ctx); err != nil {
		conn.Close()
		return nil, err
	}
	return conn, nil
}

func IsMySQL(conn *sql.DB) bool {
	_, ok := conn.Driver().(*mysql.MySQLDriver)
	return ok
}

func BeginWrite(ctx context.Context, conn *sql.DB) (*sql.Tx, error) {
	if IsMySQL(conn) {
		return conn.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelReadCommitted})
	}
	return conn.BeginTx(ctx, nil)
}

func LockRows(conn *sql.DB, query string) string {
	if IsMySQL(conn) {
		return query + " FOR UPDATE"
	}
	return query
}

func IsDuplicate(err error) bool {
	var driverErr *mysql.MySQLError
	return errors.As(err, &driverErr) && driverErr.Number == 1062
}
