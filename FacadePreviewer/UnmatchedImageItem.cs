using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Media;

namespace FacadePreviewer;

/// <summary>One image that stitch_folder.py flagged in {facade}_unmatched_images.json as never
/// actually contributing to the stitched result (see MainViewModel.UnmatchedImages, populated
/// after a scan completes). Two distinct reasons, both from the pipeline's own report rather than
/// how the photo looks (e.g. "it's tilted") -- see 2026-09-08 CLAUDE.local.md entry:
///   - "1차 매칭 실패": zero pairwise geometry edge passed the quality gate with any other image,
///     so it never even reached COLMAP.
///   - "COLMAP 등록 실패": reached COLMAP but COLMAP itself couldn't register it.
/// IsSelected mirrors CapturedFrameItem.IsIncluded's role but inverted polarity -- checked here
/// means "yes, exclude this one" (default true, since being in this list already means the
/// pipeline itself flagged it as unused) -- see MainViewModel.ExcludeUnmatchedImages.</summary>
public sealed class UnmatchedImageItem : INotifyPropertyChanged
{
    public string FilePath { get; }
    public string FileName => System.IO.Path.GetFileName(FilePath);
    public string Reason { get; }

    private bool _isSelected = true;
    public bool IsSelected
    {
        get => _isSelected;
        set
        {
            if (_isSelected == value)
                return;
            _isSelected = value;
            OnPropertyChanged();
        }
    }

    public ImageSource? ThumbnailSource { get; init; }

    public UnmatchedImageItem(string filePath, string reason)
    {
        FilePath = filePath;
        Reason = reason;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
}
