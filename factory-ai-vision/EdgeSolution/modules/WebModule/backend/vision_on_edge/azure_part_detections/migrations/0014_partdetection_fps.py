# Generated by Django 3.0.8 on 2020-09-24 08:09

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("azure_part_detections", "0013_auto_20200924_0613"),
    ]

    operations = [
        migrations.AddField(
            model_name="partdetection",
            name="fps",
            field=models.IntegerField(default=10),
        ),
    ]