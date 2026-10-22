CREATE DATABASE IF NOT EXISTS medical_crisis;
USE medical_crisis;
CREATE TABLE IF NOT EXISTS users (
  id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(120) NOT NULL, email VARCHAR(120) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL, role VARCHAR(20) NOT NULL, organization_name VARCHAR(150),
  phone VARCHAR(30), address VARCHAR(255), categories VARCHAR(255), created_at DATETIME);
CREATE TABLE IF NOT EXISTS inventory (
  id INT AUTO_INCREMENT PRIMARY KEY, hospital_id INT NOT NULL, item_name VARCHAR(120) NOT NULL, category VARCHAR(50) NOT NULL,
  description TEXT, quantity INT NOT NULL DEFAULT 0, minimum_quantity INT NOT NULL DEFAULT 0, unit VARCHAR(30),
  batch_number VARCHAR(60), expiry_date DATE, supplier VARCHAR(120), is_cold_chain TINYINT(1) DEFAULT 0,
  min_temperature FLOAT, max_temperature FLOAT, current_temperature FLOAT, temp_updated DATETIME,
  created_at DATETIME, updated_at DATETIME, FOREIGN KEY (hospital_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS requirements (
  id INT AUTO_INCREMENT PRIMARY KEY, hospital_id INT NOT NULL, item_name VARCHAR(120) NOT NULL, category VARCHAR(50),
  required_quantity INT NOT NULL, current_quantity INT NOT NULL DEFAULT 0, priority VARCHAR(10), required_by DATE,
  description TEXT, status VARCHAR(30) DEFAULT 'Pending', created_at DATETIME, FOREIGN KEY (hospital_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS supply_requests (
  id INT AUTO_INCREMENT PRIMARY KEY, hospital_id INT NOT NULL, supplier_id INT NOT NULL, requirement_id INT,
  item_name VARCHAR(120) NOT NULL, requested_quantity INT NOT NULL, supplied_quantity INT DEFAULT 0, priority VARCHAR(10),
  status VARCHAR(30) DEFAULT 'PENDING', request_date DATETIME, required_date DATE, supplier_response TEXT,
  created_at DATETIME, updated_at DATETIME, FOREIGN KEY (hospital_id) REFERENCES users(id),
  FOREIGN KEY (supplier_id) REFERENCES users(id), FOREIGN KEY (requirement_id) REFERENCES requirements(id));
CREATE TABLE IF NOT EXISTS alerts (
  id INT AUTO_INCREMENT PRIMARY KEY, hospital_id INT NOT NULL, inventory_id INT, alert_type VARCHAR(30) NOT NULL,
  severity VARCHAR(15) NOT NULL, message VARCHAR(400) NOT NULL, is_read TINYINT(1) DEFAULT 0, created_at DATETIME,
  FOREIGN KEY (hospital_id) REFERENCES users(id), FOREIGN KEY (inventory_id) REFERENCES inventory(id));

CREATE TABLE IF NOT EXISTS inter_hospital_requests (
  id INT AUTO_INCREMENT PRIMARY KEY,
  requester_hospital_id INT NOT NULL,
  provider_hospital_id INT NOT NULL,
  source_inventory_id INT NULL,
  item_name VARCHAR(120) NOT NULL,
  category VARCHAR(50) NOT NULL,
  unit VARCHAR(30),
  batch_number VARCHAR(60),
  requested_quantity INT NOT NULL,
  reason TEXT NOT NULL,
  priority VARCHAR(10) NOT NULL DEFAULT 'NORMAL',
  status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
  created_at DATETIME NOT NULL,
  responded_at DATETIME,
  FOREIGN KEY (requester_hospital_id) REFERENCES users(id),
  FOREIGN KEY (provider_hospital_id) REFERENCES users(id),
  FOREIGN KEY (source_inventory_id) REFERENCES inventory(id) ON DELETE SET NULL
);
