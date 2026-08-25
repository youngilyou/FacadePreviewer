using System.Globalization;
using System.Windows.Data;

namespace FacadePreviewer.Converters;

/// <summary>Used to disable "측정 장소"/"캡처 저장 위치" edit boxes while a capture is running --
/// same "stop to change" convention as the DDS-Router fields (see MainViewModel's doc
/// comments), enforced visually instead of just documented.</summary>
public sealed class InverseBoolConverter : IValueConverter
{
    public object Convert(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is bool b ? !b : value!;

    public object ConvertBack(object? value, Type targetType, object? parameter, CultureInfo culture)
        => value is bool b ? !b : value!;
}
