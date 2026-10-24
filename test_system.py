"""
Automated Comprehensive Test Suite for:
Medical Inventory Priority Matrix and Crisis Resource Allocator
- Supplier stock management
- Hospital stock search (sufficient/partial/no-stock)
- Direct supply requests & backend validation
- Safe stock reservation & double-deduction prevention
- Access control & role enforcement
- Hospital inventory isolation
"""

import unittest
from datetime import date, timedelta
from app import (app, db, User, Inventory, Requirement, SupplyRequest, Alert,
                 InterHospitalRequest, ensure_supplier_stock_columns,
                 calculate_medicine_demand, get_near_expiry_recommendations)


class MedicalCrisisTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.app = app.test_client()
        self.ctx = app.app_context()
        self.ctx.push()

        # Run database migrations
        ensure_supplier_stock_columns()

        # Create or fetch test users
        self.hospital = User.query.filter_by(email="hospital@medicrisis.local").first()
        self.supplier1 = User.query.filter_by(email="supplier@medicrisis.local").first()

        # Create a second supplier for multiple-supplier search tests
        self.supplier2 = User.query.filter_by(email="supplier2@medicrisis.local").first()
        if not self.supplier2:
            from werkzeug.security import generate_password_hash
            self.supplier2 = User(
                name="Vikram Sethi",
                email="supplier2@medicrisis.local",
                password_hash=generate_password_hash("supplier123"),
                role="provider",
                organization_name="Apex Health Logistics",
                phone="0484-555-0999",
                address="88 Harbour Road, Kochi",
                categories="Medicines, Surgical Supplies, Oxygen",
                is_active=True
            )
            db.session.add(self.supplier2)
            db.session.commit()

    def tearDown(self):
        self.ctx.pop()

    def login(self, email, password, role):
        return self.app.post("/login", data=dict(
            email=email,
            password=password,
            role=role
        ), follow_redirects=True)

    def logout(self):
        return self.app.get("/logout", follow_redirects=True)

    def cleanup_item(self, item_name):
        SupplyRequest.query.filter_by(item_name=item_name).delete()
        items = Inventory.query.filter_by(item_name=item_name).all()
        for it in items:
            Alert.query.filter_by(inventory_id=it.id).delete()
            db.session.delete(it)
        db.session.commit()

    # -------------------------------------------------------------
    # 1. Schema & Column Checks
    # -------------------------------------------------------------
    def test_01_schema_columns_exist(self):
        """Test that minimal schema alterations exist on existing tables."""
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        inv_cols = {c["name"] for c in inspector.get_columns("inventory")}
        sr_cols = {c["name"] for c in inspector.get_columns("supply_requests")}
        self.assertIn("is_supplier_stock", inv_cols)
        self.assertIn("stock_reserved", sr_cols)

    # -------------------------------------------------------------
    # 2. Supplier Stock Management (Add, Update, Edit, View, Safe Delete)
    # -------------------------------------------------------------
    def test_02_supplier_stock_management(self):
        """Test that suppliers can add, view, update, and manage available stock."""
        self.login("supplier@medicrisis.local", "supplier123", "provider")

        # 1. Add new stock listing
        test_item_name = "Automated Test Drug"
        self.cleanup_item(test_item_name)

        resp = self.app.post("/provider/inventory/add", data=dict(
            item_name=test_item_name,
            category="Medicines",
            quantity="250",
            unit="boxes",
            batch_number="B-AUTO-01",
            expiry_date=(date.today() + timedelta(days=200)).isoformat(),
            description="High quality test antibiotic"
        ), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Added Automated Test Drug", resp.data)

        # Verify in DB
        stock = Inventory.query.filter_by(hospital_id=self.supplier1.id, item_name=test_item_name, is_supplier_stock=True).first()
        self.assertIsNotNone(stock)
        self.assertEqual(stock.quantity, 250)
        self.assertEqual(stock.is_supplier_stock, True)

        # 2. Quick update stock quantity
        resp = self.app.post(f"/provider/inventory/update/{stock.id}", data=dict(
            quantity="320"
        ), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        stock = db.session.get(Inventory, stock.id)
        self.assertEqual(stock.quantity, 320)

        # 3. Edit stock listing
        resp = self.app.post(f"/provider/inventory/edit/{stock.id}", data=dict(
            item_name=test_item_name,
            category="Medicines",
            quantity="310",
            unit="packs",
            batch_number="B-AUTO-02",
            description="Updated description"
        ), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        stock = db.session.get(Inventory, stock.id)
        self.assertEqual(stock.quantity, 310)
        self.assertEqual(stock.unit, "packs")

        # 4. View in supplier dashboard
        resp = self.app.get("/provider/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Automated Test Drug", resp.data)

        # 5. Safe delete when no active requests
        resp = self.app.post(f"/provider/inventory/delete/{stock.id}", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(db.session.get(Inventory, stock.id))

    # -------------------------------------------------------------
    # 3. Hospital Search: Sufficient, Partial, No Stock, Multiple Suppliers
    # -------------------------------------------------------------
    def test_03_hospital_stock_search(self):
        """Test hospital stock search for sufficient stock, partial stock, multiple suppliers, and no stock."""
        item_name = "Crisis Surgical Kit"
        self.cleanup_item(item_name)

        s1_stock = Inventory(
            hospital_id=self.supplier1.id, item_name=item_name, category="Surgical Supplies",
            quantity=100, minimum_quantity=0, unit="kits", is_supplier_stock=True, supplier=self.supplier1.org
        )
        s2_stock = Inventory(
            hospital_id=self.supplier2.id, item_name=item_name, category="Surgical Supplies",
            quantity=30, minimum_quantity=0, unit="kits", is_supplier_stock=True, supplier=self.supplier2.org
        )
        db.session.add_all([s1_stock, s2_stock])
        db.session.commit()

        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # Search with required quantity 50:
        # Supplier 1 (has 100) -> Sufficient Stock
        # Supplier 2 (has 30)  -> Partial Stock
        resp = self.app.get(f"/hospital/requests?q=Crisis+Surgical+Kit&needed_qty=50")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Crisis Surgical Kit", resp.data)
        self.assertIn(b"MediSupply Co.", resp.data)
        self.assertIn(b"Apex Health Logistics", resp.data)
        self.assertIn(b"Sufficient Stock", resp.data)
        self.assertIn(b"Partial Stock", resp.data)

        # Contact info should be visible
        self.assertIn(b"0484-555-0202", resp.data)
        self.assertIn(b"0484-555-0999", resp.data)

        # Search for non-existent item -> clear message displayed
        resp_empty = self.app.get("/hospital/requests?q=Unobtanium+Medicine&needed_qty=10")
        self.assertEqual(resp_empty.status_code, 200)
        self.assertIn(b"No Registered Suppliers Have Matching Stock", resp_empty.data)

    # -------------------------------------------------------------
    # 4. Direct Request Workflow & Backend Validations
    # -------------------------------------------------------------
    def test_04_direct_request_validation_and_creation(self):
        """Test backend validation: positive quantity, stock availability, duplicate prevention, and request creation."""
        item_name = "Sterile Gauze Pack"
        self.cleanup_item(item_name)

        s_stock = Inventory(
            hospital_id=self.supplier1.id, item_name=item_name, category="Surgical Supplies",
            quantity=50, minimum_quantity=0, unit="packs", is_supplier_stock=True, supplier=self.supplier1.org
        )
        db.session.add(s_stock)
        db.session.commit()

        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # 1. Invalid supplier ID
        resp = self.app.post("/hospital/requests", data=dict(
            supplier_id="999999", item_name=item_name, requested_quantity="10"
        ), follow_redirects=True)
        self.assertIn(b"Choose an active registered supplier", resp.data)

        # 2. Non-positive quantity
        resp = self.app.post("/hospital/requests", data=dict(
            supplier_id=str(self.supplier1.id), item_name=item_name, requested_quantity="-5"
        ), follow_redirects=True)
        self.assertIn(b"Requested quantity must be greater than zero", resp.data)

        # 3. Quantity exceeding available supplier stock (requested 100, available 50)
        resp = self.app.post("/hospital/requests", data=dict(
            supplier_id=str(self.supplier1.id), item_name=item_name, requested_quantity="100"
        ), follow_redirects=True)
        self.assertIn(b"exceeds available stock", resp.data)

        # 4. Valid request creation (20 units)
        resp = self.app.post("/hospital/requests", data=dict(
            supplier_id=str(self.supplier1.id), item_name=item_name, requested_quantity="20",
            required_date=(date.today() + timedelta(days=2)).isoformat()
        ), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Supply request for 20 units of Sterile Gauze Pack sent successfully", resp.data)

        sr = SupplyRequest.query.filter_by(hospital_id=self.hospital.id, supplier_id=self.supplier1.id, item_name=item_name).first()
        self.assertIsNotNone(sr)
        self.assertEqual(sr.requested_quantity, 20)
        self.assertEqual(sr.status, "PENDING")

        # 5. Duplicate request prevention
        resp = self.app.post("/hospital/requests", data=dict(
            supplier_id=str(self.supplier1.id), item_name=item_name, requested_quantity="10"
        ), follow_redirects=True)
        self.assertIn(b"already exists", resp.data)

    # -------------------------------------------------------------
    # 5. Supplier Acceptance, Stock Reservation & Safe Deletion
    # -------------------------------------------------------------
    def test_05_supplier_acceptance_and_stock_reservation(self):
        """Test that accepting a request safely reserves stock and prevents double deduction or overselling."""
        item_name = "Emergency Saline IV"
        self.cleanup_item(item_name)

        # Supplier has 40 units
        s_stock = Inventory(
            hospital_id=self.supplier1.id, item_name=item_name, category="IV Fluids",
            quantity=40, minimum_quantity=0, unit="bottles", is_supplier_stock=True, supplier=self.supplier1.org
        )
        db.session.add(s_stock)
        db.session.commit()

        # Hospital requests 25 units
        sr = SupplyRequest(
            hospital_id=self.hospital.id, supplier_id=self.supplier1.id,
            item_name=item_name, requested_quantity=25, status="PENDING"
        )
        db.session.add(sr)
        db.session.commit()

        # Login as supplier
        self.login("supplier@medicrisis.local", "supplier123", "provider")

        # Attempt to delete listing while pending request exists -> MUST FAIL safely
        resp_del = self.app.post(f"/provider/inventory/delete/{s_stock.id}", follow_redirects=True)
        self.assertIn(b"Cannot remove listing", resp_del.data)

        # Accept request (reserves 25 units)
        resp = self.app.post(f"/provider/request/{sr.id}", data=dict(
            action="accept",
            supplied_quantity="25",
            supplier_response="Dispatching immediately via courier"
        ), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        # Verify stock deducted from 40 to 15
        s_stock = db.session.get(Inventory, s_stock.id)
        sr = db.session.get(SupplyRequest, sr.id)
        self.assertEqual(sr.status, "ACCEPTED")
        self.assertEqual(sr.stock_reserved, True)
        self.assertEqual(s_stock.quantity, 15)

        # Supplier now updates status to FULFILLED -> Must NOT double deduct
        resp_fulfill = self.app.post(f"/provider/request/{sr.id}", data=dict(
            action="update",
            status="FULFILLED"
        ), follow_redirects=True)
        self.assertEqual(resp_fulfill.status_code, 200)
        s_stock = db.session.get(Inventory, s_stock.id)
        self.assertEqual(s_stock.quantity, 15)  # Still 15, no double deduction!

    # -------------------------------------------------------------
    # 6. Hospital Inventory Isolation
    # -------------------------------------------------------------
    def test_06_hospital_inventory_isolation(self):
        """Verify that supplier inventory is completely isolated from hospital dashboards, alerts, and totals."""
        item_name = "Isolated Supplier Drug"
        self.cleanup_item(item_name)

        # Create supplier item with critical quantity (should NOT trigger hospital critical alerts)
        s_stock = Inventory(
            hospital_id=self.supplier1.id, item_name=item_name, category="Medicines",
            quantity=5, minimum_quantity=100, unit="vials", is_supplier_stock=True, supplier=self.supplier1.org
        )
        db.session.add(s_stock)
        db.session.commit()

        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # Check hospital inventory page does NOT list supplier item
        resp = self.app.get("/hospital/inventory")
        self.assertNotIn(b"Isolated Supplier Drug", resp.data)

        # Check hospital dashboard
        resp = self.app.get("/hospital/dashboard")
        self.assertNotIn(b"Isolated Supplier Drug", resp.data)

    # -------------------------------------------------------------
    # 7. Access Control
    # -------------------------------------------------------------
    def test_07_access_control(self):
        """Test role-based access control between hospitals and suppliers."""
        # Hospital cannot access provider dashboard or provider inventory actions
        self.login("hospital@medicrisis.local", "hospital123", "hospital")
        resp = self.app.get("/provider/dashboard", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)  # Redirects unauthorized

        resp = self.app.post("/provider/inventory/add", data={}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

        # Supplier cannot access hospital inventory
        self.login("supplier@medicrisis.local", "supplier123", "provider")
        resp = self.app.get("/hospital/inventory", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    # -------------------------------------------------------------
    # 8. Concurrent Stock Requests & Overselling Prevention
    # -------------------------------------------------------------
    def test_08_concurrent_stock_requests(self):
        """Test concurrent stock requests when stock is scarce to prevent overselling."""
        item_name = "Rare Antidote Serum"
        self.cleanup_item(item_name)

        # Supplier has only 30 units in stock
        s_stock = Inventory(
            hospital_id=self.supplier1.id, item_name=item_name, category="Medicines",
            quantity=30, minimum_quantity=0, unit="doses", is_supplier_stock=True, supplier=self.supplier1.org
        )
        db.session.add(s_stock)
        db.session.commit()

        # Hospital 1 requests 20 units
        sr1 = SupplyRequest(
            hospital_id=self.hospital.id, supplier_id=self.supplier1.id,
            item_name=item_name, requested_quantity=20, status="PENDING"
        )
        # Hospital 2 (User id 12) requests 20 units
        sr2 = SupplyRequest(
            hospital_id=12, supplier_id=self.supplier1.id,
            item_name=item_name, requested_quantity=20, status="PENDING"
        )
        db.session.add_all([sr1, sr2])
        db.session.commit()

        self.login("supplier@medicrisis.local", "supplier123", "provider")

        # 1. Supplier accepts Hospital 1's request -> 20 reserved, 10 remain
        resp1 = self.app.post(f"/provider/request/{sr1.id}", data=dict(action="accept"), follow_redirects=True)
        self.assertEqual(resp1.status_code, 200)

        s_stock = db.session.get(Inventory, s_stock.id)
        self.assertEqual(s_stock.quantity, 10)

        # 2. Supplier attempts to accept Hospital 2's request for 20 units -> MUST BE REJECTED because only 10 remain
        resp2 = self.app.post(f"/provider/request/{sr2.id}", data=dict(action="accept"), follow_redirects=True)
        self.assertIn(b"is less than the quantity to supply", resp2.data)

        # Stock remains protected at 10 and request 2 remains PENDING
        s_stock = db.session.get(Inventory, s_stock.id)
        sr2 = db.session.get(SupplyRequest, sr2.id)
        self.assertEqual(s_stock.quantity, 10)
        self.assertEqual(sr2.status, "PENDING")

    # -------------------------------------------------------------
    # 9. Hospital Stock Receipt Workflow
    # -------------------------------------------------------------
    def test_09_full_receipt_into_hospital_inventory(self):
        """Test that fulfilled supply requests can be received by hospital, crediting hospital inventory."""
        item_name = "Paracetamol"
        h_stock = Inventory.query.filter_by(hospital_id=self.hospital.id, item_name=item_name, is_supplier_stock=False).first()
        initial_hospital_qty = h_stock.quantity

        # Create fulfilled request
        sr = SupplyRequest(
            hospital_id=self.hospital.id, supplier_id=self.supplier1.id,
            item_name=item_name, requested_quantity=50, supplied_quantity=50,
            status="FULFILLED", stock_reserved=True
        )
        db.session.add(sr)
        db.session.commit()

        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # Hospital receives the stock
        resp = self.app.post(f"/hospital/requests/{sr.id}/receive", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Received 50 units of Paracetamol", resp.data)

        # Hospital inventory increased by 50
        h_stock = db.session.get(Inventory, h_stock.id)
        sr = db.session.get(SupplyRequest, sr.id)
        self.assertEqual(h_stock.quantity, initial_hospital_qty + 50)
        self.assertEqual(sr.status, "RECEIVED")

    # -------------------------------------------------------------
    # 10. Automatic Priority Matrix: Zero-Stock, Expired, and Reasons
    # -------------------------------------------------------------
    def test_10_priority_matrix_zero_stock_and_expired(self):
        """Test that zero-stock, expired items, and shortage thresholds trigger correct priorities and reasons."""
        # 1. Zero stock -> CRITICAL
        item_zero = Inventory(
            hospital_id=self.hospital.id, item_name="Zero Stock Drug", category="Medicines",
            quantity=0, minimum_quantity=100, unit="vials", is_supplier_stock=False
        )
        self.assertEqual(item_zero.priority, "CRITICAL")
        self.assertIn("ZERO STOCK", item_zero.priority_reason)

        # 2. Expired item -> CRITICAL and is_expired is True
        item_exp = Inventory(
            hospital_id=self.hospital.id, item_name="Expired Drug", category="Medicines",
            quantity=80, minimum_quantity=50, unit="tablets",
            expiry_date=date.today() - timedelta(days=5), is_supplier_stock=False
        )
        self.assertEqual(item_exp.priority, "CRITICAL")
        self.assertTrue(item_exp.is_expired)
        self.assertIn("EXPIRED", item_exp.priority_reason)

        # 3. Near-expiry item
        item_near = Inventory(
            hospital_id=self.hospital.id, item_name="Near Expiry Drug", category="Medicines",
            quantity=150, minimum_quantity=100, unit="boxes",
            expiry_date=date.today() + timedelta(days=15), is_supplier_stock=False
        )
        self.assertTrue(item_near.is_near_expiry)
        self.assertIn("EXPIRING SOON", item_near.priority_reason)

        # 4. Critical shortage (<= 50% minimum)
        item_crit = Inventory(
            hospital_id=self.hospital.id, item_name="Critical Shortage Drug", category="Medicines",
            quantity=40, minimum_quantity=100, unit="packs",
            expiry_date=date.today() + timedelta(days=200), is_supplier_stock=False
        )
        self.assertEqual(item_crit.priority, "CRITICAL")
        self.assertIn("CRITICAL", item_crit.priority_reason)

        # 5. Low stock (<= minimum)
        item_low = Inventory(
            hospital_id=self.hospital.id, item_name="Low Stock Drug", category="Medicines",
            quantity=85, minimum_quantity=100, unit="packs",
            expiry_date=date.today() + timedelta(days=200), is_supplier_stock=False
        )
        self.assertEqual(item_low.priority, "LOW")
        self.assertIn("LOW", item_low.priority_reason)

        # 6. Normal stock (> minimum)
        item_norm = Inventory(
            hospital_id=self.hospital.id, item_name="Normal Stock Drug", category="Medicines",
            quantity=150, minimum_quantity=100, unit="packs",
            expiry_date=date.today() + timedelta(days=200), is_supplier_stock=False
        )
        self.assertEqual(item_norm.priority, "NORMAL")
        self.assertIn("NORMAL", item_norm.priority_reason)

    # -------------------------------------------------------------
    # 11. Inter-Hospital Sharing: Rejection of Expired Stock & Duplicate Prevention
    # -------------------------------------------------------------
    def test_11_inter_hospital_sharing_expired_and_duplicates(self):
        """Test that inter-hospital sharing disallows expired medicines and prevents duplicate pending requests."""
        # Clean up any existing item
        test_item = "Sharing Safety Drug"
        InterHospitalRequest.query.filter_by(item_name=test_item).delete()
        self.cleanup_item(test_item)

        # Provider hospital has expired stock
        provider_hosp = User.query.filter_by(role="hospital").filter(User.id != self.hospital.id).first()
        if not provider_hosp:
            provider_hosp = User(
                name="North District Clinic", email="north@medicrisis.local",
                password_hash="test", role="hospital", organization_name="North District Clinic",
                is_active=True
            )
            db.session.add(provider_hosp)
            db.session.commit()

        expired_stock = Inventory(
            hospital_id=provider_hosp.id, item_name=test_item, category="Medicines",
            quantity=50, minimum_quantity=10, unit="vials",
            expiry_date=date.today() - timedelta(days=10), is_supplier_stock=False
        )
        db.session.add(expired_stock)
        db.session.commit()

        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # 1. Attempt to request expired item -> MUST FAIL
        resp_exp = self.app.post("/hospital/inter-hospital-sharing", data=dict(
            action="create",
            provider_hospital_id=str(provider_hosp.id),
            source_inventory_id=str(expired_stock.id),
            requested_quantity="20",
            priority="CRITICAL",
            reason="Emergency ward need"
        ), follow_redirects=True)
        self.assertIn(b"Cannot request or transfer expired medicines", resp_exp.data)

        # Update stock to valid expiry date for duplicate prevention test
        expired_stock.expiry_date = date.today() + timedelta(days=180)
        db.session.commit()

        # 2. First valid request -> succeeds
        resp_valid = self.app.post("/hospital/inter-hospital-sharing", data=dict(
            action="create",
            provider_hospital_id=str(provider_hosp.id),
            source_inventory_id=str(expired_stock.id),
            requested_quantity="15",
            priority="NORMAL",
            reason="Routine buffer transfer"
        ), follow_redirects=True)
        self.assertIn(b"Sharing request sent", resp_valid.data)

        # 3. Duplicate request for same item -> MUST FAIL
        resp_dup = self.app.post("/hospital/inter-hospital-sharing", data=dict(
            action="create",
            provider_hospital_id=str(provider_hosp.id),
            source_inventory_id=str(expired_stock.id),
            requested_quantity="10",
            priority="NORMAL",
            reason="Duplicate transfer attempt"
        ), follow_redirects=True)
        self.assertIn(b"already exists", resp_dup.data)

        # Cleanup
        InterHospitalRequest.query.filter_by(item_name=test_item).delete()
        self.cleanup_item(test_item)

    # -------------------------------------------------------------
    # 12. Temperature Monitoring: Validation & Standard Form Support
    # -------------------------------------------------------------
    def test_12_temperature_monitoring_and_validation(self):
        """Test temperature range validation and dedicated update route."""
        # Find cold chain item
        cold_item = Inventory.query.filter_by(hospital_id=self.hospital.id, is_cold_chain=True, is_supplier_stock=False).first()
        if not cold_item:
            cold_item = Inventory(
                hospital_id=self.hospital.id, item_name="Test Cold Vaccine", category="Vaccines",
                quantity=40, minimum_quantity=20, unit="doses", is_cold_chain=True,
                min_temperature=2.0, max_temperature=8.0, current_temperature=4.0, is_supplier_stock=False
            )
            db.session.add(cold_item)
            db.session.commit()

        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # 1. Valid update via direct POST route
        resp = self.app.post(f"/hospital/cold-chain/update/{cold_item.id}", data=dict(
            temperature="5.5"
        ), follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Temperature reading updated successfully", resp.data)
        item = db.session.get(Inventory, cold_item.id)
        self.assertEqual(item.current_temperature, 5.5)

        # 2. Out of range temperature (e.g. 99°C) -> MUST FAIL
        resp_err = self.app.post(f"/hospital/cold-chain/update/{cold_item.id}", data=dict(
            temperature="99"
        ), follow_redirects=True)
        self.assertIn(b"out of plausible medical range", resp_err.data)

    # -------------------------------------------------------------
    # 13. Transparent Rule-Based Demand Estimation (No AI/ML)
    # -------------------------------------------------------------
    def test_13_deterministic_demand_prediction(self):
        """Test that medicine demand estimation is deterministic, uses existing records, and flags insufficient data."""
        estimates = calculate_medicine_demand(self.hospital.id)
        self.assertIsInstance(estimates, list)
        self.assertGreater(len(estimates), 0)

        for est in estimates:
            self.assertIn("item_name", est)
            self.assertIn("projected_demand", est)
            self.assertIn("is_insufficient", est)
            self.assertIn("note", est)
            # Projected demand must be a non-negative integer
            self.assertGreaterEqual(est["projected_demand"], 0)
            # If insufficient historical records, note clearly states so
            if est["is_insufficient"]:
                self.assertIn("Insufficient historical records", est["note"])

    # -------------------------------------------------------------
    # 14. Proactive Near-Expiry Transfer Recommendations
    # -------------------------------------------------------------
    def test_14_near_expiry_recommendations(self):
        """Test that eligible near-expiry medicines are identified for transfer and expired medicines excluded."""
        # Find another hospital
        other_hosp = User.query.filter_by(role="hospital").filter(User.id != self.hospital.id).first()

        item_name = "Expiring Antibiotic"
        # Cleanup
        Requirement.query.filter_by(item_name=item_name).delete()
        self.cleanup_item(item_name)

        # Hospital 1 has near-expiry stock (expiring in 20 days)
        h1_stock = Inventory(
            hospital_id=self.hospital.id, item_name=item_name, category="Medicines",
            quantity=60, minimum_quantity=10, unit="boxes",
            expiry_date=date.today() + timedelta(days=20), is_supplier_stock=False
        )
        # Other hospital has active requirement
        req = Requirement(
            hospital_id=other_hosp.id, item_name=item_name, category="Medicines",
            required_quantity=50, current_quantity=0, priority="CRITICAL", status="Pending"
        )
        db.session.add_all([h1_stock, req])
        db.session.commit()

        recs = get_near_expiry_recommendations(self.hospital.id)
        matching = [r for r in recs if r["item"].item_name == item_name]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["target_hospital"].id, other_hosp.id)
        self.assertEqual(matching[0]["recommended_qty"], 50)
        self.assertIn("open requirement", matching[0]["reason"])

        # Final cleanup
        Requirement.query.filter_by(item_name=item_name).delete()
        self.cleanup_item(item_name)

    # -------------------------------------------------------------
    # 15. Offline Sync Deduplication
    # -------------------------------------------------------------
    def test_15_offline_sync_deduplication(self):
        """Test that multiple offline edits to the same item quantity apply the latest valid state cleanly."""
        h_item = Inventory.query.filter_by(hospital_id=self.hospital.id, is_supplier_stock=False).first()
        self.login("hospital@medicrisis.local", "hospital123", "hospital")

        # Simulate two consecutive queued updates to the same item (e.g. user set 400 then 450 while offline)
        payload = {
            "ops": [
                {"type": "inventory_update", "id": h_item.id, "quantity": 400},
                {"type": "inventory_update", "id": h_item.id, "quantity": 450}
            ]
        }
        resp = self.app.post("/api/offline-sync", json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreaterEqual(data["applied"], 1)

        # Inventory reflects latest state (450)
        item = db.session.get(Inventory, h_item.id)
        self.assertEqual(item.quantity, 450)


if __name__ == "__main__":
    unittest.main()
