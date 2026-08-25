using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Media;

namespace FacadePreviewer;

/// <summary>One thumbnail in TransferSettingsWindow's pre-transfer review panel. IsIncluded is
/// the single source of truth for "will this file actually be sent" -- unchecking it never
/// touches the original file on disk (an explicit project decision: these are un-backed-up raw
/// drone captures at review time, so exclusion here must be reversible up until 전송 is clicked).
/// See TransferSettingsWindow.xaml.cs's GetExcludedFileSet/PrepareTransferFolder for where this
/// actually gets enforced against the transfer.</summary>
public sealed class ReviewImageItem : INotifyPropertyChanged
{
    public string FilePath { get; }
    public string FileName => System.IO.Path.GetFileName(FilePath);

    private bool _isIncluded = true;
    public bool IsIncluded
    {
        get => _isIncluded;
        set
        {
            if (_isIncluded == value)
                return;
            _isIncluded = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(Opacity));
        }
    }

    // Dims (not hides) an unchecked thumbnail so the operator can still see what it looked like
    // while reviewing the rest -- "선택 제외" (OnReviewExcludeClick) is what actually removes it
    // from view, this alone does not.
    public double Opacity => IsIncluded ? 1.0 : 0.35;

    // Set by a single click (see TransferSettingsWindow.xaml.cs's OnThumbnailClicked) -- purely a
    // visual highlight plus what "원본 보기" (ViewOriginalButton) acts on; unrelated to IsIncluded.
    // Added alongside the double-click-to-view behavior, not instead of it (design review: "버튼
    // 하나 넣고 스위치 위치되게 해도 됨" -- a dedicated button is an acceptable alternative/addition
    // to double-click, not a replacement for it).
    private bool _isSelected;
    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (_isSelected == value)
                return;
            _isSelected = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(SelectionBorderBrush));
        }
    }

    public Brush SelectionBorderBrush => IsSelected ? Brushes.OrangeRed : Brushes.Transparent;

    public ImageSource? ThumbnailSource { get; init; }

    public ReviewImageItem(string filePath)
    {
        FilePath = filePath;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}
