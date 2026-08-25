using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Media;

namespace FacadePreviewer;

/// <summary>One live-captured frame in MainWindow's left-sidebar capture list (see
/// MainViewModel.CapturedFrames). IsIncluded is the single source of truth for "will this frame
/// actually be handed to stitch_folder.py" -- checked by default (same convention as
/// TransferSettingsWindow's own ReviewImageItem, kept consistent across both review UIs per
/// explicit request). Unchecking never deletes the file; "선택 제외" (see
/// MainViewModel.RemoveExcludedFrames) is what actually moves every currently-unchecked frame's
/// file into the capture folder's "excluded" subfolder (never deletes it) and removes it from the
/// list, so RunScan's directory-wide glob skips it.</summary>
public sealed class CapturedFrameItem : INotifyPropertyChanged
{
    // Not read-only: RemoveExcludedFrames updates this after physically moving the file.
    public string FilePath { get; set; }
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
        }
    }

    public ImageSource? ThumbnailSource { get; init; }

    public CapturedFrameItem(string filePath)
    {
        FilePath = filePath;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}
