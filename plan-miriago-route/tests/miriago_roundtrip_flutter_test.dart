import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:miriago/plan_transfer/plan_import_package.dart';

void main() {
  test('MiriaGo imports the planner output from a real export', () {
    final packagePath = Platform.environment['MIRIAGO_IMPORT_CHECK_PACKAGE'];
    expect(packagePath, isNotNull);
    final imported = readPlanImportPackageFromBytes(
      File(packagePath!).readAsBytesSync(),
      sourceName: packagePath,
    );
    final plan = imported.package.plan;
    expect(imported.schemaVersion, 2);
    expect(plan.groups, isNotEmpty);
    expect(plan.points, isNotEmpty);
    expect(plan.points.where((point) => point.groupId != null), isNotEmpty);
    expect(
      plan.groups.map((group) => group.orderIndex).toList(),
      List<int>.generate(plan.groups.length, (index) => index),
    );
  });
}
