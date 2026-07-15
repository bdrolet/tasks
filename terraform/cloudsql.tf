# The instance is created and owned by the INBOX repo's terraform (moving to a
# platform state in ~/src/infra later — see
# /Users/ben/.claude/plans/infra-platform-migration.md). This repo only adds
# its own database and user on it. A database on an existing instance costs
# nothing; the instance is the billable unit.
data "google_sql_database_instance" "inbox" {
  name = "inbox"
}

resource "google_sql_database" "tasks" {
  instance = data.google_sql_database_instance.inbox.name
  name     = "tasks"
}

resource "google_sql_user" "tasks" {
  instance = data.google_sql_database_instance.inbox.name
  name     = "tasks"
  password = var.tasks_db_password
}
