import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:miriago/plan_transfer/plan_import_package.dart';

void main() {
  test('MiriaGo importer accepts the planned integration package', () {
    final packagePath = Platform.environment['MIRIAGO_IMPORT_CHECK_PACKAGE'];
    expect(packagePath, isNotNull);
    final imported = readPlanImportPackageFromBytes(
      File(packagePath!).readAsBytesSync(),
      sourceName: packagePath,
    );
    final plan = imported.package.plan;
    expect(imported.schemaVersion, 2);
    expect(plan.id, 'plan-1');
    expect(plan.works, hasLength(1));
    expect(plan.groups, hasLength(2));
    expect(plan.points, hasLength(4));
    expect(imported.package.visitRecords, hasLength(1));
    expect(plan.points.where((point) => point.groupId != null), hasLength(3));
    expect(imported.assetEntries, hasLength(2));
  });
}
