import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:miriago/data/sample_pilgrimage_repository.dart';
import 'package:miriago/plan_transfer/plan_export_v2.dart';
import 'package:miriago/plan_transfer/plan_import_package.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'MiriaGo exporter writes a real fixture for planner integration',
    () async {
      final outputPath = Platform.environment['MIRIAGO_EXPORT_FIXTURE_PATH'];
      expect(outputPath, isNotNull);
      final repository = SamplePilgrimageRepository();
      final plan = await repository.loadActivePlan();
      final exported = await buildPlanExportV2Package(
        plan: plan,
        visitRecords: const [],
        options: const PlanExportV2Options(
          mode: PlanExportV2Mode.planOnly,
          includeFullReferenceCache: false,
        ),
      );
      await File(outputPath!).writeAsBytes(exported.bytes);
      final imported = readPlanImportPackageFromBytes(
        exported.bytes,
        sourceName: exported.fileName,
      );
      expect(imported.schemaVersion, 2);
      expect(imported.package.plan.points, isNotEmpty);
    },
  );
}
