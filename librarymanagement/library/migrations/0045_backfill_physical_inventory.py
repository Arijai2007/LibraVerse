from django.contrib.auth.hashers import make_password
from django.db import migrations


def backfill(apps, schema_editor):
    db = schema_editor.connection.alias
    Book = apps.get_model("library", "Book")
    Copy = apps.get_model("library", "BookCopy")
    Loan = apps.get_model("library", "IssuedBook")
    Author = apps.get_model("library", "Author")
    Category = apps.get_model("library", "Category")
    User = apps.get_model("auth", "User")

    # This exact account was created by migration 0031 with a known password.
    # Keep its user ID and ownership so existing foreign keys remain intact.
    User.objects.using(db).filter(
        username="default_migration_admin",
        email="default-admin@example.com",
        owned_libraries__code="LIB-DEFAULT",
    ).update(is_active=False, password=make_password(None))
    Loan.objects.using(db).all().update(legacy=True)
    for book in Book.objects.using(db).iterator(chunk_size=250):
        if book.author:
            book.author_record, _ = Author.objects.using(db).get_or_create(
                library_id=book.library_id,
                name=book.author,
            )
        if book.category:
            book.category_record, _ = Category.objects.using(db).get_or_create(
                library_id=book.library_id,
                name=book.category,
            )
        book.save(using=db, update_fields=["author_record", "category_record"])
        for start in range(0, book.quantity, 500):
            Copy.objects.using(db).bulk_create(
                [Copy(book_id=book.pk) for _ in range(min(500, book.quantity - start))]
            )
        for loan in (
            Loan.objects.using(db)
            .filter(book=book)
            .select_related("student")
            .iterator()
        ):
            fields = []
            if (
                loan.student
                and loan.student.library_id == book.library_id
                and loan.student.user_id
            ):
                loan.borrower_id = loan.student.user_id
                fields.append("borrower")
            if not loan.returned:
                loan.copy = Copy.objects.using(db).create(book=book, status="borrowed")
                fields.append("copy")
            if fields:
                loan.save(using=db, update_fields=fields)


class Migration(migrations.Migration):
    dependencies = [("library", "0044_physical_circulation")]
    operations = [
        migrations.RunPython(backfill)
    ]  # New copy history cannot be losslessly reversed.
