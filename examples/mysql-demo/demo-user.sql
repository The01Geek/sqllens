-- Runs after Chinook_MySql.sql on the container's first start.
-- Demo-only credentials: the user can read the Chinook database and nothing else.
CREATE USER 'chinook_reader'@'%' IDENTIFIED BY 'chinook_reader';
GRANT SELECT ON `Chinook`.* TO 'chinook_reader'@'%';
