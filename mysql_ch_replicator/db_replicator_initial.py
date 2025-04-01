import json
import os.path
import hashlib
import time
import sys
import subprocess
from logging import getLogger
from enum import Enum

from .config import Settings
from .mysql_api import MySQLApi
from .clickhouse_api import ClickhouseApi
from .converter import MysqlToClickhouseConverter
from .table_structure import TableStructure
from .utils import touch_all_files
from .common import Status

logger = getLogger(__name__)

class DbReplicatorInitial:
    
    INITIAL_REPLICATION_BATCH_SIZE = 50000
    SAVE_STATE_INTERVAL = 10
    BINLOG_TOUCH_INTERVAL = 120

    def __init__(self, replicator):
        self.replicator = replicator
        self.last_touch_time = 0
        self.last_save_state_time = 0

    def create_initial_structure(self):
        self.replicator.state.status = Status.CREATING_INITIAL_STRUCTURES
        for table in self.replicator.state.tables:
            self.create_initial_structure_table(table)
        self.replicator.state.save()

    def create_initial_structure_table(self, table_name):
        if not self.replicator.config.is_table_matches(table_name):
            return

        if self.replicator.single_table and self.replicator.single_table != table_name:
            return

        mysql_create_statement = self.replicator.mysql_api.get_table_create_statement(table_name)
        mysql_structure = self.replicator.converter.parse_mysql_table_structure(
            mysql_create_statement, required_table_name=table_name,
        )
        self.validate_mysql_structure(mysql_structure)
        clickhouse_structure = self.replicator.converter.convert_table_structure(mysql_structure)
        
        # Always set if_not_exists to True to prevent errors when tables already exist
        clickhouse_structure.if_not_exists = True
        
        self.replicator.state.tables_structure[table_name] = (mysql_structure, clickhouse_structure)
        indexes = self.replicator.config.get_indexes(self.replicator.database, table_name)

        if not self.replicator.is_parallel_worker:
            self.replicator.clickhouse_api.create_table(clickhouse_structure, additional_indexes=indexes)

    def validate_mysql_structure(self, mysql_structure: TableStructure):
        for key_idx in mysql_structure.primary_key_ids:
            primary_field = mysql_structure.fields[key_idx]
            if 'not null' not in primary_field.parameters.lower():
                logger.warning('primary key validation failed')
                logger.warning(
                    f'\n\n\n    !!!  WARNING - PRIMARY KEY NULLABLE (field "{primary_field.name}", table "{mysql_structure.table_name}") !!!\n\n'
                    'There could be errors replicating nullable primary key\n'
                    'Please ensure all tables has NOT NULL parameter for primary key\n'
                    'Or mark tables as skipped, see "exclude_tables" option\n\n\n'
                )

    def prevent_binlog_removal(self):
        if time.time() - self.last_touch_time < self.BINLOG_TOUCH_INTERVAL:
            return
        binlog_directory = os.path.join(self.replicator.config.binlog_replicator.data_dir, self.replicator.database)
        logger.info(f'touch binlog {binlog_directory}')
        if not os.path.exists(binlog_directory):
            return
        self.last_touch_time = time.time()
        touch_all_files(binlog_directory)

    def save_state_if_required(self, force=False):
        curr_time = time.time()
        if curr_time - self.last_save_state_time < self.SAVE_STATE_INTERVAL and not force:
            return
        self.last_save_state_time = curr_time
        self.replicator.state.tables_last_record_version = self.replicator.clickhouse_api.tables_last_record_version
        self.replicator.state.save()

    def perform_initial_replication(self):
        self.replicator.clickhouse_api.database = self.replicator.target_database_tmp
        logger.info('running initial replication')
        self.replicator.state.status = Status.PERFORMING_INITIAL_REPLICATION
        self.replicator.state.save()
        start_table = self.replicator.state.initial_replication_table
        for table in self.replicator.state.tables:
            if start_table and table != start_table:
                continue
            if self.replicator.single_table and self.replicator.single_table != table:
                continue
            self.perform_initial_replication_table(table)
            start_table = None

        if not self.replicator.is_parallel_worker:
            logger.info(f'initial replication - swapping database')
            if self.replicator.target_database in self.replicator.clickhouse_api.get_databases():
                self.replicator.clickhouse_api.execute_command(
                    f'RENAME DATABASE `{self.replicator.target_database}` TO `{self.replicator.target_database}_old`',
                )
                self.replicator.clickhouse_api.execute_command(
                    f'RENAME DATABASE `{self.replicator.target_database_tmp}` TO `{self.replicator.target_database}`',
                )
                self.replicator.clickhouse_api.drop_database(f'{self.replicator.target_database}_old')
            else:
                self.replicator.clickhouse_api.execute_command(
                    f'RENAME DATABASE `{self.replicator.target_database_tmp}` TO `{self.replicator.target_database}`',
                )
            self.replicator.clickhouse_api.database = self.replicator.target_database
        logger.info(f'initial replication - done')

    def perform_initial_replication_table(self, table_name):
        logger.info(f'running initial replication for table {table_name}')

        if not self.replicator.config.is_table_matches(table_name):
            logger.info(f'skip table {table_name} - not matching any allowed table')
            return

        if not self.replicator.is_parallel_worker and self.replicator.config.initial_replication_threads > 1:
            self.replicator.state.initial_replication_table = table_name
            self.replicator.state.initial_replication_max_primary_key = None
            self.replicator.state.save()
            self.perform_initial_replication_table_parallel(table_name)
            return

        max_primary_key = None
        if self.replicator.state.initial_replication_table == table_name:
            # continue replication from saved position
            max_primary_key = self.replicator.state.initial_replication_max_primary_key
            logger.info(f'continue from primary key {max_primary_key}')
        else:
            # starting replication from zero
            logger.info(f'replicating from scratch')
            self.replicator.state.initial_replication_table = table_name
            self.replicator.state.initial_replication_max_primary_key = None
            self.replicator.state.save()

        mysql_table_structure, clickhouse_table_structure = self.replicator.state.tables_structure[table_name]

        logger.debug(f'mysql table structure: {mysql_table_structure}')
        logger.debug(f'clickhouse table structure: {clickhouse_table_structure}')

        field_types = [field.field_type for field in clickhouse_table_structure.fields]

        primary_keys = clickhouse_table_structure.primary_keys
        primary_key_ids = clickhouse_table_structure.primary_key_ids
        primary_key_types = [field_types[key_idx] for key_idx in primary_key_ids]

        stats_number_of_records = 0
        last_stats_dump_time = time.time()

        while True:

            query_start_values = max_primary_key
            if query_start_values is not None:
                for i in range(len(query_start_values)):
                    key_type = primary_key_types[i]
                    value = query_start_values[i]
                    if 'int' not in key_type.lower():
                        value = f"'{value}'"
                        query_start_values[i] = value

            records = self.replicator.mysql_api.get_records(
                table_name=table_name,
                order_by=primary_keys,
                limit=self.INITIAL_REPLICATION_BATCH_SIZE,
                start_value=query_start_values,
                worker_id=self.replicator.worker_id,
                total_workers=self.replicator.total_workers,
            )
            logger.debug(f'extracted {len(records)} records from mysql')

            records = self.replicator.converter.convert_records(records, mysql_table_structure, clickhouse_table_structure)

            if self.replicator.config.debug_log_level:
                logger.debug(f'records: {records}')

            if not records:
                break
            self.replicator.clickhouse_api.insert(table_name, records, table_structure=clickhouse_table_structure)
            for record in records:
                record_primary_key = [record[key_idx] for key_idx in primary_key_ids]
                if max_primary_key is None:
                    max_primary_key = record_primary_key
                else:
                    max_primary_key = max(max_primary_key, record_primary_key)

            self.replicator.state.initial_replication_max_primary_key = max_primary_key
            self.save_state_if_required()
            self.prevent_binlog_removal()

            stats_number_of_records += len(records)
            curr_time = time.time()
            if curr_time - last_stats_dump_time >= 60.0:
                last_stats_dump_time = curr_time
                logger.info(
                    f'replicating {table_name}, '
                    f'replicated {stats_number_of_records} records, '
                    f'primary key: {max_primary_key}',
                )

        logger.info(
            f'finish replicating {table_name}, '
            f'replicated {stats_number_of_records} records, '
            f'primary key: {max_primary_key}',
        )

    def perform_initial_replication_table_parallel(self, table_name):
        """
        Execute initial replication for a table using multiple parallel worker processes.
        Each worker will handle a portion of the table based on its worker_id and total_workers.
        """
        logger.info(f"Starting parallel replication for table {table_name} with {self.replicator.config.initial_replication_threads} workers")

        # Create and launch worker processes
        processes = []
        for worker_id in range(self.replicator.config.initial_replication_threads):
            # Prepare command to launch a worker process
            cmd = [
                sys.executable, "-m", "mysql_ch_replicator.main",
                "db_replicator",  # Required positional mode argument
                "--config", self.replicator.settings_file,
                "--db", self.replicator.database,
                "--worker_id", str(worker_id),
                "--total_workers", str(self.replicator.config.initial_replication_threads),
                "--table", table_name,
                "--target_db", self.replicator.target_database_tmp,
                "--initial_only=True",
            ]
                
            logger.info(f"Launching worker {worker_id}: {' '.join(cmd)}")
            process = subprocess.Popen(cmd)
            processes.append(process)
        
        # Wait for all worker processes to complete
        logger.info(f"Waiting for {len(processes)} workers to complete replication of {table_name}")
        
        try:
            while processes:
                for i, process in enumerate(processes[:]):
                    # Check if process is still running
                    if process.poll() is not None:
                        exit_code = process.returncode
                        if exit_code == 0:
                            logger.info(f"Worker process {i} completed successfully")
                        else:
                            logger.error(f"Worker process {i} failed with exit code {exit_code}")
                            # Optional: can raise an exception here to abort the entire operation
                            raise Exception(f"Worker process failed with exit code {exit_code}")
                        
                        processes.remove(process)
                
                if processes:
                    # Wait a bit before checking again
                    time.sleep(0.1)
                    
                    # Every 30 seconds, log progress
                    if int(time.time()) % 30 == 0:
                        logger.info(f"Still waiting for {len(processes)} workers to complete")
        except KeyboardInterrupt:
            logger.warning("Received interrupt, terminating worker processes")
            for process in processes:
                process.terminate()
            raise
        
        logger.info(f"All workers completed replication of table {table_name}")
