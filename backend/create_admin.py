from sqlalchemy import select

import _startup
from app import create_app
from database import models
from database import SessionLocal
from database.models import User, Role, Permission
from sqlalchemy import select

app = create_app()

ALL_PERMISSIONS = [
    "users:view", "users:create", "users:edit", "users:delete",
    "roles:manage",
    "orders:view", "orders:create", "orders:edit", "orders:delete", "orders:mark_invoiced", "orders:mark_paid",
    "qb:pull_orders", "qb:sync_catalog",
    "inventory:view", "inventory:allocate", "inventory:fulfill", "inventory:manual_adjust",
    "transactions:rollback",
    "catalog:view", "catalog:create", "catalog:edit", "catalog:archive",
    "tracker:view",
    "tracker:update_sales", "tracker:update_service",
    "tracker:update_logistics", "tracker:update_delivery",
    "conversions:view", "conversions:create", "conversions:edit", "conversions:rollback"
]

def seed_database():
    db = SessionLocal()
    with app.app_context():
        print("1. Seeding Granular Permissions...")
        db_permissions = []
        for perm_name in ALL_PERMISSIONS:
            # Check if permission already exists
            perm = db.execute(select(Permission).where(Permission.name == perm_name)).scalar_one_or_none()
            if not perm:
                perm = Permission(name=perm_name, description=f"Allows action: {perm_name}")
                db.add(perm)
            db_permissions.append(perm)
            
        db.commit()

        print("2. Seeding Admin Role...")
        admin_role = db.execute(select(Role).where(Role.name == "Admin")).scalar_one_or_none()
        if not admin_role:
            admin_role = Role(name="Admin", description="Master account with full system access.")
            db.add(admin_role)
            
        # Grant the Admin role EVERY permission in the system
        admin_role.permissions = db_permissions
        db.commit()

        print("3. Seeding Admin User...")
        email = "admin@afc.com"  # Change this to your preferred admin email
        admin_user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        
        if not admin_user:
            admin_user = User(
                email=email,
                role_id=admin_role.id,
                is_active=True
            )
            # ⚠️ Change this password immediately after logging in!
            admin_user.set_password("AFC1110") 
            db.add(admin_user)
            db.commit()
            print(f"✅ Successfully created Admin user: {email}")
        else:
            # If you ran this before and the user exists, just update their role_id
            admin_user.role_id = admin_role.id
            db.commit()
            print(f"✅ Admin user {email} already exists. Updated role to Admin.")

        print("\n🚀 Database RBAC seeding complete!")

if __name__ == "__main__":
    seed_database()

app.run(host="0.0.0.0", port=5000, debug=False)