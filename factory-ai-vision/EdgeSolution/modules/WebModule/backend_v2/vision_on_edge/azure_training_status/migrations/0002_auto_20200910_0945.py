# Generated by Django 3.0.8 on 2020-09-10 09:45

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('azure_training_status', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='trainingstatus',
            name='log',
            field=models.CharField(blank=True, default='Status : Has not configured', max_length=1000),
        ),
        migrations.AlterField(
            model_name='trainingstatus',
            name='status',
            field=models.CharField(blank=True, default='ok', max_length=200),
        ),
    ]
